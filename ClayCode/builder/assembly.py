#!/usr/bin/env python3

import re
import shutil
import tempfile
from functools import cached_property
from pathlib import Path
from typing import Optional, List, Union, Callable

import numpy as np
import pandas as pd

from ClayCode import logger, MDP
from ClayCode.builder.claycomp import UCData
from ClayCode.core.consts import GRO_FMT
from ClayCode.builder.topology import TopologyConstructorBase
from ClayCode.core.classes import FileFactory, GROFile, TOPFile, Dir
from ClayCode.core.gmx import run_gmx_insert_mols, run_gmx_solvate
from ClayCode.core.lib import (
    add_resnum,
    add_ions_n_mols,
    write_insert_dat,
    center_clay,
    add_ions_neutral,
    select_outside_clay_stack,
    check_insert_numbers,
    run_em,
    set_mdp_freeze_clay,
    set_mdp_parameter,
)
from ClayCode.core.utils import get_header, get_subheader
from MDAnalysis import Universe, Merge, AtomGroup, ResidueGroup
from MDAnalysis.units import constants
from numpy._typing import NDArray

__all__ = ["Builder", "Sheet"]


class Builder:
    __tmp_outpath = tempfile.TemporaryDirectory()
    __tmp_file = tempfile.NamedTemporaryFile(dir=__tmp_outpath.name, delete=False)

    def __init__(self, build_args):
        self.args = build_args
        self.sheet = Sheet(
            self.args._uc_data,
            uc_ids=self.args.sheet_uc_weights.index.values,
            uc_numbers=self.args.sheet_uc_weights.values,
            x_cells=self.args.x_cells,
            y_cells=self.args.y_cells,
            fstem=self.args.filestem,
            outpath=self.args.outpath,
        )
        self.top = TopologyConstructorBase(self.args._uc_data, self.args.ff)
        self.__il_solv = None
        self.__stack = None
        self.__box_ext = False
        logger.info(get_header(f"Building {self.args.name} model"))
        logger.info(
            f"{self.args.n_sheets} sheets\n"
            f"Sheet dimensions: "
            f"{self.sheet.x_cells * self.sheet.uc_dimensions[0]:.2f} A X {self.sheet.y_cells * self.sheet.uc_dimensions[1]:.2f} A "
            f"({self.sheet.x_cells} unit cells X {self.sheet.y_cells} unit cells)\n"
            f"Box height: {self.args.box_height:.1f} A"
        )

    @property
    def extended_box(self) -> bool:
        return self.__box_ext

    def solvate_clay_sheets(self) -> None:
        logger.info(get_subheader(f"2. Generating interlayer solvent."))
        solvent: Solvent = Solvent(
            x_dim=self.sheet.dimensions[0],
            y_dim=self.sheet.dimensions[1],
            n_mols=self.args.n_waters,
            z_dim=self.args.il_solv_height,
        )
        spc_file: GROFile = self.get_filename("interlayer", suffix=".gro")
        solvent.write(spc_name=spc_file, topology=self.top)
        self.il_solv: GROFile = spc_file
        # self.il_solv = spc_file
        logger.info(f"Writing interlayer sheet to {self.il_solv.name!r}\n")

    def rename_il_solv(self) -> None:
        # crdout = self.get_filename('solv', 'ions', 'iSL', suffix='.gro')
        il_u: Universe = Universe(str(self.il_solv))
        il_resnames: NDArray = il_u.residues.resnames
        il_resnames: list = list(
            map(lambda resname: re.sub("SOL", "iSL", resname), il_resnames)
        )
        il_u.residues.resnames: NDArray = il_resnames
        self.il_solv.universe: Universe = il_u
        self.il_solv.write()
        self.top.reset_molecules()
        self.top.add_molecules(il_u)
        self.top.write(self.il_solv.top)

    def run_em(self):
        logger.info(get_subheader("Minimising energy"))
        em_inp = "em.mdp"
        em_filestr = set_mdp_freeze_clay(
            uc_stem=self.args.uc_stem,
            uc_list=self.args.sheet_uc_ids,
            em_template=em_inp,
            freeze_dims=["Y", "Y", "N"],
        )
        em_filestr = set_mdp_parameter("constraints", "h-bonds", em_filestr)
        em_filestr = set_mdp_parameter("emstep", "0.0001", em_filestr)
        em_filestr = set_mdp_parameter("emtol", "1000", em_filestr)
        with tempfile.NamedTemporaryFile(
            mode="w+",
            prefix=Path(em_inp).stem,
            suffix=".mdp",
            delete=False,
            dir=self.args.outpath,
        ) as mdp_file:
            mdp_file.write(em_filestr)
            result = run_em(
                mdp=self.args.outpath / mdp_file.name,
                crdin=self.stack,
                topin=self.stack.top,
                odir=self.args.outpath,
                outname=self.stack.stem,
            )
        outpath = Dir(self.args.outpath)
        crd_top_files = [
            *outpath.itp_filelist,
            *outpath._get_filelist(ext=".top"),
            *outpath.gro_filelist,
        ]
        for file in outpath.iterdir():
            if file not in crd_top_files:
                file.unlink(missing_ok=True)
        return result

    def conclude(self):
        # run_em(
        #     mdp=MDP / "no_run.mdp",
        #     crdin=self.stack,
        #     topin=self.stack.top,
        #     odir=self.args.outpath,
        #     outname=self.stack.stem,
        # )
        logger.info(get_subheader("Finishing up"))
        self.stack: GROFile = self.args.outpath / self.stack.name
        self.__tmp_outpath.cleanup()
        logger.info(
            f"Wrote final coordinates and topology to {self.stack.name!r} and {self.stack.top.name!r}"
        )
        logger.info(get_header(f"{self.args.name} model setup complete"))

    def remove_il_solv(self) -> None:
        logger.info(f"Removing interlayer solvent")
        il_u: Universe = Universe(str(self.il_solv))
        il_atoms: AtomGroup = il_u.select_atoms("not resname SOL iSL")
        self.il_solv.universe = il_atoms
        self.il_solv.write(topology=self.top)

    def extend_box(self) -> None:
        if type(self.args.box_height) in [int, float]:
            if self.args.box_height > self.stack.universe.dimensions[2]:
                logger.info(f"Extending simulation box to {self.args.box_height:.1f} A")
                self.__box_ext: bool = True
                ext_boxname: GROFile = self.get_filename("ext", suffix=".gro")
                box_u: Universe = self.stack.universe
                box_u.universe.dimensions[2] = self.args.box_height
                self.stack: GROFile = ext_boxname
                self.stack.universe = box_u
                self.stack.write(topology=self.top)

                logger.info(f"Saving extended box as {self.stack.stem!r}\n")
            else:
                self.__box_ext: bool = False

    def remove_SOL(self) -> None:
        self.stack.reset_universe()
        box_u: Universe = self.stack.universe
        box_u: AtomGroup = box_u.select_atoms("not resname SOL")
        self.stack.universe = box_u
        add_resnum(crdin=self.stack, crdout=self.stack)
        self.stack.reset_universe()
        self.stack.write(topology=self.top)

    def solvate_box(self, extra=2) -> None:
        if self.extended_box is True:
            logger.info("Adding bulk solvation:")
            solv_box_crd: GROFile = self.get_filename("solv", suffix=".gro")

            self.remove_SOL()
            run_gmx_solvate(
                p=self.stack.top,
                pp=solv_box_crd.top,
                cp=self.stack,
                radius=0.2,
                scale=0.57,
                o=solv_box_crd,
                maxsol=0,
                box="{} {} {}".format(*self.stack.universe.dimensions[:3] * 0.10),
            )
            # self.stack = solv_box_crd
            # solv_box_u = self.stack.universe
            solv_box_u: Universe = solv_box_crd.universe.copy()
            not_sol: AtomGroup = solv_box_u.select_atoms(f"not resname SOL")
            sol: AtomGroup = solv_box_u.select_atoms(f"resname SOL")
            _sol = self.select_molecules_outside_clay(sol, extra=extra)
            # logger.info(f'\tInserted {sol.n_atoms} {np.unique(sol.resnames)[0]} molecules')
            logger.info(
                f"\tInserted {_sol.n_atoms} {np.unique(_sol.resnames)[0]} molecules"
            )
            sol = _sol
            solv_box_u: AtomGroup = not_sol + sol
            solv_box_crd.universe: Union[Universe, AtomGroup, ResidueGroup] = solv_box_u
            solv_box_crd.write(self.top)

            self.stack: GROFile = solv_box_crd
            self.stack.write(self.top)
            # self.top.reset_molecules()
            # self.top.add_molecules(solv_box_u)
            # self.top.write(self.stack.top)
            logger.info(f"Saving solvated box as {self.stack.stem!r}\n")
        else:
            logger.info("Skipping bulk solvation.\n")

    @cached_property
    def __ion_sel_str(self) -> str:
        return " ".join(self.args.ff["ions"]["atomtypes"].df["at-type"])

    def remove_bulk_ions(self):
        stack_u = self.stack.universe
        il_ions = stack_u.select_atoms(f"resname {self.__ion_sel_str}")
        il_ions = self.select_molecules_outside_clay(il_ions, extra=0)
        stack_atoms = stack_u.atoms - il_ions
        self.stack.universe = stack_atoms
        self.stack.write(topology=self.top)

    @property
    def clay(self):
        return self.stack.universe.select_atoms(f"resname {self.args.uc_stem}*")

    def select_molecules_outside_clay(
        self, atomgroup: AtomGroup, extra: Union[int, float] = 0
    ) -> AtomGroup:
        atom_group: AtomGroup = select_outside_clay_stack(
            atom_group=atomgroup, clay=self.clay, extra=extra
        )
        residue_groups = atom_group.split("residue")
        for residue_group in residue_groups:
            if residue_group.n_atoms != residue_group.residues.atoms.n_atoms:
                atom_group -= residue_group
        return atom_group

    @property
    def clay_min(self) -> float:
        return np.min(self.clay.positions[:, 2])

    @property
    def clay_max(self) -> float:
        return np.max(self.clay.positions[:, 2])

    def add_bulk_ions(self) -> None:
        if self.extended_box is True:
            logger.info(f"Adding bulk ions:")
            outcrd: GROFile = self.get_filename("solv", "ions", suffix=".gro")
            shutil.copy(self.stack, outcrd)
            outcrd.write(topology=self.top)
            self.stack: GROFile = outcrd
            logger.debug(f"before n_atoms: {self.stack.universe.atoms.n_atoms}")
            self.remove_bulk_ions()
            logger.debug(f"after n_atoms: {self.stack.universe.atoms.n_atoms}")

            ion_df: pd.DataFrame = self.args.bulk_ion_df
            pion: str = self.args.default_bulk_pion[0]
            # pq = int(self.args.default_bulk_pion[1])
            nion: str = self.args.default_bulk_nion[0]
            # nq = int(self.args.default_bulk_nion[1])
            bulk_x, bulk_y, bulk_z = self.stack.universe.dimensions[:3]
            bulk_z -= np.abs(self.clay_max - self.clay_min)
            for ion, values in ion_df.iterrows():
                charge, conc = values
                n_ions: int = np.rint(
                    bulk_z * bulk_x * bulk_y * constants["N_Avogadro"] * conc * 1e-27
                ).astype(
                    int
                )  # 1 mol/L = 10^-27 mol/A
                logger.info(f"\tAdding {conc} mol/L ({n_ions} atoms) {ion} to bulk")
                logger.debug(f"before n_atoms: {self.stack.universe.atoms.n_atoms}")
                replaced: int = add_ions_n_mols(
                    odir=self.__tmp_outpath.name,
                    crdin=self.stack,
                    # crdout=self.stack,
                    topin=self.stack.top,
                    # topout=self.stack.top,
                    ion=ion,
                    charge=int(charge),
                    n_atoms=n_ions,
                )
                logger.debug(f"after n_atoms: {self.stack.universe.atoms.n_atoms}")
                logger.info(f"\t\tReplaced {replaced} SOL molecules with {ion}")
                self.stack.reset_universe()
                self.stack.write(self.top)
            logger.info(f"\tNeutralising with {pion} and {nion}")
            replaced: int = add_ions_neutral(
                odir=self.__tmp_outpath.name,
                crdin=self.stack,
                # crdout=self.stack,
                topin=self.stack.top,
                # topout=self.stack.top,
                nion=nion,
                # nq=nq,
                pion=pion,
                # pq=pq)
            )
            logger.debug(f"n_atoms: {self.stack.universe.atoms.n_atoms}")
            logger.info(f"\t\tReplaced {replaced} SOL molecules")
            logger.info(f"Saving solvated box with ions as {self.stack.stem!r}")
            self.stack.reset_universe()
            self.stack.write(self.top)
            processed_top = Path("processed.top")
            processed_top.unlink(missing_ok=True)
        else:
            logger.info("\tSkipping bulk ion addition.")

    def stack_sheets(self, extra=0.5) -> None:
        try:
            il_crds: GROFile = self.il_solv
            il_u = il_crds.universe
            il_solv = True
        except AttributeError:
            il_solv = False
        sheet_universes = []
        sheet_heights = []
        if il_solv is not False:
            logger.info(get_subheader("3. Assembling box"))
            logger.info("Combining clay sheets and interlayer")
        else:
            logger.info(f"Combining clay sheets\n")
        for sheet_id in range(self.args.n_sheets):
            self.sheet.n_sheet = sheet_id
            sheet_u = self.sheet.universe.copy()
            if il_solv is not False:
                il_u_copy = il_u.copy()
                if sheet_id == self.args.n_sheets - 1:
                    il_u_copy.residues.resnames = list(
                        map(
                            lambda resname: re.sub("iSL", "SOL", resname),
                            il_u_copy.residues.resnames,
                        )
                    )
                il_u_copy.atoms.translate([0, 0, sheet_u.dimensions[2] + extra])
                new_dimensions: NDArray = sheet_u.dimensions
                sheet_u: Universe = Merge(sheet_u.atoms, il_u_copy.atoms)
                sheet_u.dimensions = new_dimensions
                sheet_u.dimensions[2] = (
                    sheet_u.dimensions[2] + il_u_copy.dimensions[2] + extra
                )
                sheet_u.atoms.translate(
                    [0, 0, sheet_id * (sheet_u.dimensions[2] + extra)]
                )
                sheet_u.dimensions[2]: float = sheet_u.dimensions[2] + extra
            else:
                sheet_u.atoms.translate([0, 0, sheet_id * sheet_u.dimensions[2]])
            sheet_universes.append(sheet_u.atoms.copy())
            sheet_heights.append(sheet_u.dimensions[2])
        combined: Universe = Merge(*sheet_universes)
        combined.dimensions = sheet_u.dimensions
        new_dimensions = combined.dimensions
        new_dimensions[2] = np.sum(sheet_heights)
        new_dimensions[3:] = [90.0, 90.0, 90.0]
        combined.dimensions = new_dimensions
        combined.atoms.pack_into_box(box=combined.dimensions, inplace=True)
        # combined.atoms.write(str(crdout))
        crdout: GROFile = self.get_filename(suffix=".gro")
        crdout.universe: Universe = combined
        crdout.write(self.top)
        add_resnum(crdin=crdout, crdout=crdout)
        self.stack: GROFile = crdout
        logger.info(f"Saving sheet stack as {self.stack.stem!r}\n")

    def __path_getter(self, property_name) -> GROFile:
        path = getattr(self, f"__{property_name}")
        if path is not None:
            return path
        else:
            logger.debug("No sheet stack filename defined.")

    @property
    def stack(self) -> GROFile:
        return self.__path_getter("stack")

    @stack.setter
    def stack(self, stack: Union[Path, str, GROFile]) -> None:
        self.__path_setter_copy("stack", stack)

    def write_sheet_crds(self) -> None:
        logger.info(get_subheader(f"1. Generating clay sheets."))
        for sheet_id in range(self.args.n_sheets):
            self.sheet.n_sheet: int = sheet_id
            self.sheet.write_gro()
        self.sheet.n_sheet = None

    def write_sheet_top(self) -> None:
        for sheet_id in range(self.args.n_sheets):
            self.top.reset_molecules()
            self.sheet.n_sheet: int = sheet_id
            self.top.add_molecules(self.sheet.universe)
            self.top.write(self.sheet.get_filename(suffix=".top"))
        self.sheet.n_sheet = None

    def get_filename(
        self, *solv_ion_args, suffix=None, sheetnum: Optional[int] = None, tcb_spec=None
    ) -> Union[GROFile, TOPFile]:
        if sheetnum is not None:
            sheetnum: str = f"_{int(sheetnum)}"
        else:
            sheetnum: str = ""
        if tcb_spec is not None:
            if tcb_spec in ["T", "C", "B"]:
                tcb_spec: str = f"_{tcb_spec}"
            else:
                raise ValueError(
                    f'{tcb_spec} was given for "tcb". Accepted '
                    '"tcb_spec" values are "T", "C", "B".'
                )
        else:
            tcb_spec = ""
        solv_ion_list: list = ["solv", "ions"]
        arg_list: list = [s for s in solv_ion_list if s in solv_ion_args]
        other_args: set = set(solv_ion_args) - set(arg_list)
        for a in sorted(other_args):
            arg_list.append(a)
        fstem: str = f"{self.args.filestem}{sheetnum}{tcb_spec}"
        fstem: str = "_".join([fstem, *arg_list])
        try:
            suffix: str = suffix.strip(".")
            suffix: str = f".{suffix}"
        except AttributeError:
            suffix: str = ""
        # global __tmp_outpath
        # __tmp_outpath = tempfile.TemporaryDirectory(dir=self.args.outpath)
        logger.debug(
            f"{self.__tmp_outpath.name} exists: {Path(self.__tmp_outpath.name).is_dir()}"
        )
        # self.__tempfile = tempfile.NamedTemporaryFile(dir=outpath, delete=False)
        path: Union[TOPFile, GROFile] = FileFactory(
            f"{self.__tmp_outpath.name}/{fstem}{suffix}"
        )
        return path

    @property
    def il_solv(self) -> GROFile:
        return self.__path_getter("il_solv")
        # if self.__il_solv is not None:
        #     return self.__il_solv
        # else:
        #     logger.debug(f'No solvation specified')

    @il_solv.setter
    def il_solv(self, il_solv: Union[Path, str, GROFile]) -> None:
        self.__path_setter_copy("il_solv", il_solv)

    def __path_setter_copy(
        self, property_name: str, file: Union[Path, str, GROFile]
    ) -> None:
        path: GROFile = getattr(self, property_name, None)
        if path is not None:
            shutil.copy(path, self.args.outpath / path.name)
            logger.debug(
                f"\nResetting {property_name}\nCopied {path.name} to {self.args.outpath.name}"
            )
            try:
                shutil.copy(path.top, self.args.outpath / path.top.name)
            except FileNotFoundError:
                path.write(topology=self.top)
                shutil.copy(path.top, self.args.outpath / path.top.name)
            finally:
                logger.debug(
                    logger.debug(f"Copied {path.top.name} to {self.args.outpath.name}")
                )
        setattr(self, f"__{property_name}", FileFactory(Path(file).with_suffix(".gro")))

    def add_il_ions(self) -> None:
        if self.il_solv is None:
            self.solvate_clay_sheets()
        logger.info(f"Adding interlayer ions:")  # to {self.il_solv.name!r}')
        infile: GROFile = self.il_solv
        # outfile = self.get_filename('solv', 'ions', suffix='gro')
        # import tempfile
        with tempfile.NamedTemporaryFile(suffix=self.il_solv.suffix) as temp_outfile:
            temp_gro: GROFile = GROFile(temp_outfile.name)
            shutil.copy(infile, temp_gro)
            dr: NDArray = self.sheet.dimensions[:3] / 10
            dr[-1] *= 0.4
            if isinstance(self.args.n_il_ions, dict):
                for ion, n_ions in self.args.n_il_ions.items():
                    if n_ions != 0:
                        logger.info(f"\tInserting {n_ions} {ion} atoms")
                        with tempfile.NamedTemporaryFile(suffix=".gro") as ion_gro:
                            ion_u: Universe = Universe.empty(
                                n_atoms=1,
                                n_residues=1,
                                n_segments=1,
                                atom_resindex=[0],
                                residue_segindex=[0],
                                trajectory=True,
                            )
                            ion_u.add_TopologyAttr("name", [ion])
                            ion_u.add_TopologyAttr("resname", [ion])
                            ion_u.dimensions = np.array(
                                [*self.sheet.dimensions, 90, 90, 90]
                            )
                            ion_u.atoms.positions = np.zeros((3,))
                            # ion_u = insert_u.select_atoms(f'resname {ion}')
                            ion_u.atoms.write(ion_gro.name)
                            # determine positions for adding ions
                            with tempfile.NamedTemporaryFile(suffix=".dat") as posfile:
                                write_insert_dat(n_mols=n_ions, save=posfile.name)
                                assert Path(posfile.name).is_file()
                                insert_err, insert_out = run_gmx_insert_mols(
                                    f=temp_gro,
                                    ci=ion_gro.name,
                                    ip=posfile.name,
                                    nmol=n_ions,
                                    o=temp_gro,
                                    replace="SOL",
                                    dr="{} {} {}".format(*dr),
                                )
                            center_clay(crdname=temp_gro, crdout=temp_gro, uc_name=ion)
                            _ = Universe(temp_gro)
                            assert Path(temp_gro).is_file()
                            replace_check: int = check_insert_numbers(
                                add_repl="Added", searchstr=insert_err
                            )
                            if replace_check != n_ions:
                                raise ValueError(
                                    f"Number of inserted molecules ({replace_check}) does not match target number "
                                    f"({n_ions})!"
                                )
            infile.universe: Universe = temp_gro.universe
            infile.write(topology=self.top)
            self.il_solv: GROFile = infile

    def center_clay_in_box(self) -> None:
        if self.__box_ext is True:
            logger.info("\nCentering clay in box")
            center_clay(self.stack, self.stack, uc_name=self.args.uc_stem)


class Sheet:
    def __init__(
        self,
        uc_data: UCData,
        uc_ids: List[int],
        uc_numbers: List[int],
        x_cells: int,
        y_cells: int,
        fstem: str,
        outpath: Path,
        n_sheet: int = None,
    ):
        self.uc_data: UCData = uc_data
        self.uc_ids: list = uc_ids
        self.uc_numbers: list = uc_numbers
        self.dimensions: NDArray = self.uc_data.dimensions[:3] * [x_cells, y_cells, 1]
        # self.dimensions[:3] *=
        self.x_cells: int = x_cells
        self.y_cells: int = y_cells
        self.fstem: str = fstem
        self.outpath: Path = outpath
        self.__n_sheet = None
        self.__random = None

    def get_filename(self, suffix: str) -> Union[GROFile, TOPFile]:
        return FileFactory(self.outpath / f"{self.fstem}_{self.n_sheet}{suffix}")

    @property
    def n_sheet(self) -> Union[int, None]:
        if self.__n_sheet is not None:
            return self.__n_sheet
        else:
            raise AttributeError(f"No sheet number set!")

    @n_sheet.setter
    def n_sheet(self, n_sheet: int):
        self.__n_sheet: int = n_sheet
        self.__random = np.random.default_rng(n_sheet)

    @property
    def random_generator(self) -> Union[None, np.random._generator.Generator]:
        if self.__random is not None:
            return self.__random
        else:
            raise AttributeError(f"No sheet number set!")

    @property
    def uc_array(self) -> NDArray:
        uc_array: NDArray = np.repeat(self.uc_ids, self.uc_numbers)
        self.random_generator.shuffle(uc_array)  # self.__uc_array)
        return uc_array

    @property
    def filename(self) -> GROFile:
        return self.get_filename(suffix=".gro")

    def write_gro(self) -> None:
        filename: GROFile = self.filename
        if filename.is_file():
            logger.debug(
                f"\n{filename.parent}/{filename.name} already exists, creating backup."
            )
            self.backup(filename)
        gro_df: pd.DataFrame = self.uc_data.gro_df
        sheet_df = pd.concat(
            [
                gro_df.filter(regex=f"[A-Z0-9]+{uc_id}", axis=0)
                for uc_id in self.uc_array
            ]
        )
        sheet_df.reset_index(["atom-id"], inplace=True)
        sheet_df["atom-id"] = np.arange(1, len(sheet_df) + 1)
        sheet_df = sheet_df.loc[:, ["at-type", "atom-id", "x", "y", "z"]]
        sheet_n_atoms: int = len(sheet_df)
        with open(filename, "w") as grofile:
            grofile.write(f"{self.fstem} sheet {self.n_sheet}\n{sheet_n_atoms}\n")
            for idx, entry in sheet_df.reset_index().iterrows():
                line: list = entry.to_list()
                grofile.write(
                    GRO_FMT.format(
                        *re.split(r"(\d+)", line[0], maxsplit=1)[1:], *line[1:]
                    )
                )
            grofile.write(f"{self.format_dimensions(self.dimensions / 10)}\n")
        add_resnum(crdin=filename, crdout=filename)
        uc_n_atoms: NDArray = np.array(
            [self.uc_data.n_atoms[uc_id] for uc_id in self.uc_array]
        ).reshape(self.x_cells, self.y_cells)
        x_repeats: Callable = lambda n_atoms: self.__cells_shift(
            n_atoms=n_atoms, n_cells=self.x_cells
        )
        y_repeats: Callable = lambda n_atoms: self.__cells_shift(
            n_atoms=n_atoms, n_cells=self.y_cells
        )
        x_pos_shift: NDArray = np.ravel(
            np.apply_along_axis(x_repeats, arr=uc_n_atoms, axis=0), order="F"
        )
        y_pos_shift: NDArray = np.ravel(
            np.apply_along_axis(y_repeats, arr=uc_n_atoms, axis=1), order="F"
        )
        new_positions: NDArray = filename.universe.atoms.positions
        new_positions[:, 0] += self.uc_dimensions[0] * x_pos_shift
        new_positions[:, 1] += self.uc_dimensions[1] * y_pos_shift
        new_universe = filename.universe
        new_universe.atoms.positions = new_positions
        logger.info(f"Writing sheet {self.n_sheet} to {filename.name}")
        filename.universe = new_universe
        filename.write()

    def __cells_shift(self, n_cells: int, n_atoms: int) -> NDArray:
        shift: NDArray = np.atleast_2d(np.arange(n_cells)).repeat(n_atoms, axis=1)
        return shift

    @staticmethod
    def format_dimensions(dimensions: NDArray) -> str:
        return "".join([f"{dimension:12.4f}" for dimension in dimensions])

    @cached_property
    def uc_dimensions(self) -> NDArray:
        return self.uc_data.dimensions

    @property
    def universe(self) -> Universe:
        return Universe(str(self.get_filename(suffix=".gro")))

    # TODO: add n_atoms and uc data to match data

    def backup(self, filename: Path) -> None:
        sheets_backup: Path = filename.with_suffix(f"{filename.suffix}.1")
        backups = filename.parent.glob(f"*.{filename.suffix}.*")
        for backup in reversed(list(backups)):
            n_backup: int = int(backup.suffices[-1].strip("."))
            new_backup: Path = backup.with_suffix(f"{filename.suffix}.{n_backup + 1}")
            shutil.move(backup, new_backup)
        shutil.move(filename, sheets_backup)


class Solvent:
    solv_density = 1000e-27  # g/L 1L = 10E27 A^3
    mw_sol = 18

    def __init__(
        self,
        x_dim: Optional[Union[int, float]] = None,
        y_dim: Optional[Union[int, float]] = None,
        z_dim: Optional[Union[int, float]] = None,
        n_mols: Optional[Union[int]] = None,
        n_ions: Optional[Union[int]] = None,
    ):
        self.x_dim = float(x_dim)
        self.y_dim = float(y_dim)
        if z_dim is None and n_mols is not None:
            self.n_mols = int(n_mols)
            self.z_dim = self.get_solvent_sheet_height(self.n_mols)
        elif n_mols is None and z_dim is not None:
            self.z_dim = float(z_dim)
            self.n_mols = self.get_sheet_solvent_mols(self.z_dim)
        else:
            raise ValueError(f"No sheet height or number of molecules specified")
        if n_ions is None:
            self.n_ions = 0
        else:
            self.n_ions = n_ions
            self.n_mols += self.n_ions
        self.n_mols: int = int(self.n_mols)

    @property
    def universe(self) -> Universe:
        universe = getattr(self, "__universe", None)
        return universe

    @property
    def topology(self) -> TopologyConstructorBase:
        top = getattr(self, "__top", None)
        return top

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.n_mols} molecules, {self.x_dim:.2f} X {self.y_dim:.2f} X {self.z_dim:.2f} A))"

    def __str__(self) -> str:
        return self.__repr__()

    def get_solvent_sheet_height(self, mols_sol: int) -> float:
        z_dim = (self.mw_sol * mols_sol) / (
            constants["N_Avogadro"] * self.x_dim * self.y_dim * self.solv_density
        )
        return z_dim

    def get_sheet_solvent_mols(self, z_dim: Union[float, int]) -> int:
        mols_sol = (
            z_dim
            / 10
            * constants["N_Avogadro"]
            * self.x_dim
            / 10
            * self.y_dim
            / 10
            * self.solv_density
        ) / (self.mw_sol)
        return round(mols_sol, 0)

    def top_str(self) -> str:
        return f"SOL\t{self.n_mols}\n"

    def write(
        self, spc_name: GROFile, topology: Optional[TopologyConstructorBase] = None
    ) -> None:
        if spc_name.__class__.__name__ != "GROFile":
            spc_gro: GROFile = GROFile(spc_name)  # .with_suffix('.gro')
        else:
            spc_gro: GROFile = spc_name
        spc_top: TOPFile = spc_gro.top
        spc_gro.universe = Universe.empty(n_atoms=0)
        spc_gro.write(topology=topology)
        # if topology.__class__.__name__ == 'TopologyConstructorBase':
        #     topology.write(spc_topname)
        # elif topology.__class__.__name__ == 'TOPFile':
        #     spc_topname = str(topology.resolve())
        solv, out = run_gmx_solvate(
            cs="spc216",
            maxsol=self.n_mols,
            o=spc_gro,
            p=spc_top,
            scale=0.00010,
            v="",
            box=f"{self.x_dim / 10} {self.y_dim / 10} {self.z_dim / 10}",
        )
        self.check_solvent_nummols(solv)
        logger.debug(f"Saving solvent sheet as {spc_gro.stem!r}")
        self.__universe: Universe = spc_gro.universe
        self.__top: TopologyConstructorBase = topology

    def check_solvent_nummols(self, solvate_stderr: str) -> None:
        added_wat: str = re.search(
            r"(?<=Number of solvent molecules:)\s+(\d+)", solvate_stderr
        ).group(1)
        if int(added_wat) < self.n_mols:
            raise ValueError(
                f"With chosen box height, GROMACS was only able to "
                f"insert {added_wat} instead of {self.n_mols} water "
                f"molecules.\nIncrease box size!"
            )