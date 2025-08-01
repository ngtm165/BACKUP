from dataclasses import dataclass, field
from functools import cached_property
from typing import NamedTuple, TypeAlias

import numpy as np
from numpy.typing import ArrayLike
from rdkit import Chem
from rdkit.Chem import Mol
from sklearn.preprocessing import StandardScaler
from torch.utils.data import Dataset

from chemprop.data.datapoints import MolAtomBondDatapoint, MoleculeDatapoint, ReactionDatapoint
from chemprop.data.molgraph import MolGraph
from chemprop.featurizers.base import Featurizer
from chemprop.featurizers.molgraph import CGRFeaturizer, SimpleMoleculeMolGraphFeaturizer
from chemprop.featurizers.molgraph.cache import MolGraphCache, MolGraphCacheOnTheFly
from chemprop.types import Rxn


class Datum(NamedTuple):
    """a singular training data point"""

    mg: MolGraph
    V_d: np.ndarray | None
    x_d: np.ndarray | None
    y: np.ndarray | None
    weight: float
    lt_mask: np.ndarray | None
    gt_mask: np.ndarray | None


class MolAtomBondDatum(NamedTuple):
    """a singular training data point that supports atom and bond level targets"""

    mg: MolGraph
    V_d: np.ndarray | None
    E_d: np.ndarray | None
    x_d: np.ndarray | None
    ys: tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]
    weight: float
    lt_masks: tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]
    gt_masks: tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]
    constraints: tuple[np.ndarray | None, np.ndarray | None]


MolGraphDataset: TypeAlias = Dataset[Datum]
MolAtomBondGraphDataset: TypeAlias = Dataset[MolAtomBondDatum]


class _MolGraphDatasetMixin:
    def __len__(self) -> int:
        return len(self.y)

    @cached_property
    def _Y(self) -> np.ndarray:
        """the raw targets of the dataset"""
        return np.array([y for y in self.y], float).reshape(-1,1)

    @property
    def Y(self) -> np.ndarray:
        """the (scaled) targets of the dataset"""
        return self.__Y

    @Y.setter
    def Y(self, Y: ArrayLike):
        self._validate_attribute(Y, "targets")

        self.__Y = np.array(Y, float)

    # @cached_property
    # def _X_d(self) -> np.ndarray:
    #     """the raw extra descriptors of the dataset"""
    #     return np.array([d.x_d for d in self.data])

    # @property
    # def X_d(self) -> np.ndarray:
    #     """the (scaled) extra descriptors of the dataset"""
    #     return self.__X_d

    # @X_d.setter
    # def X_d(self, X_d: ArrayLike):
    #     self._validate_attribute(X_d, "extra descriptors")

    #     self.__X_d = np.array(X_d)

    @property
    def weights(self) -> np.ndarray:
        return np.array([d.weight for d in self.data])

    @property
    def gt_mask(self) -> np.ndarray:
        return np.array([d.gt_mask for d in self.data])

    @property
    def lt_mask(self) -> np.ndarray:
        return np.array([d.lt_mask for d in self.data])

    @property
    def t(self) -> int | None:
        return self.data[0].t if len(self.data) > 0 else None

    @property
    def d_xd(self) -> int:
        """the extra molecule descriptor dimension, if any"""
        return 0 if self.X_d[0] is None else self.X_d.shape[1]

    @property
    def names(self) -> list[str]:
        return [d.name for d in self.data]

    def normalize_targets(self, scaler: StandardScaler | None = None) -> StandardScaler:
        """Normalizes the targets of this dataset using a :obj:`StandardScaler`

        The :obj:`StandardScaler` subtracts the mean and divides by the standard deviation for
        each task independently. NOTE: This should only be used for regression datasets.

        Returns
        -------
        StandardScaler
            a scaler fit to the targets.
        """

        if scaler is None:
            print(self._Y)
            scaler = StandardScaler().fit(self._Y)

        self.Y = scaler.transform(self._Y)
        print(self.Y)

        return scaler

    def normalize_inputs(
        self, key: str = "X_d", scaler: StandardScaler | None = None
    ) -> StandardScaler:
        VALID_KEYS = {"X_d"}
        if key not in VALID_KEYS:
            raise ValueError(f"Invalid feature key! got: {key}. expected one of: {VALID_KEYS}")

        X = self.X_d if self.X_d[0] is not None else None

        if X is None:
            return scaler

        if scaler is None:
            scaler = StandardScaler().fit(X)

        self.X_d = scaler.transform(X)

        return scaler

    def reset(self):
        """Reset the atom and bond features; atom and extra descriptors; and targets of each
        datapoint to their initial, unnormalized values."""
        self.__Y = self._Y
        # self.__X_d = self._X_d

    def _validate_attribute(self, X: np.ndarray, label: str):
        if not len(self.v_attr) == len(X):
            raise ValueError(
                f"number of molecules ({len(self.v_attr)}) and {label} ({len(X)}) "
                "must have same length!"
            )


@dataclass
class MoleculeDataset(_MolGraphDatasetMixin, MolGraphDataset):
    """A :class:`MoleculeDataset` composed of :class:`MoleculeDatapoint`\s

    A :class:`MoleculeDataset` produces featurized data for input to a
    :class:`MPNN` model. Typically, data featurization is performed on-the-fly
    and parallelized across multiple workers via the :class:`~torch.utils.data
    DataLoader` class. However, for small datasets, it may be more efficient to
    featurize the data in advance and cache the results. This can be done by
    setting ``MoleculeDataset.cache=True``.

    Parameters
    ----------
    data : Iterable[MoleculeDatapoint]
        the data from which to create a dataset
    featurizer : MoleculeFeaturizer
        the featurizer with which to generate MolGraphs of the molecules
    """

    data: list[MoleculeDatapoint]
    featurizer: Featurizer[Mol, MolGraph] = field(default_factory=SimpleMoleculeMolGraphFeaturizer)

    def __post_init__(self):
        if self.data is None:
            raise ValueError("Data cannot be None!")

        self.reset()
        self.cache = False

    def __getitem__(self, idx: int) -> Datum:
        d = self.data[idx]
        mg = self.mg_cache[idx]

        return Datum(mg, self.V_ds[idx], self.X_d[idx], self.Y[idx], d.weight, d.lt_mask, d.gt_mask)

    @property
    def cache(self) -> bool:
        return self.__cache

    @cache.setter
    def cache(self, cache: bool = False):
        self.__cache = cache
        self._init_cache()

    def _init_cache(self):
        """initialize the cache"""
        self.mg_cache = (MolGraphCache if self.cache else MolGraphCacheOnTheFly)(
            self.mols, self.V_fs, self.E_fs, self.featurizer
        )

    @property
    def smiles(self) -> list[str]:
        """the SMILES strings associated with the dataset"""
        return [Chem.MolToSmiles(d.mol) for d in self.data]

    @property
    def mols(self) -> list[Chem.Mol]:
        """the molecules associated with the dataset"""
        return [d.mol for d in self.data]

    @property
    def _V_fs(self) -> list[np.ndarray]:
        """the raw atom features of the dataset"""
        return [d.V_f for d in self.data]

    @property
    def V_fs(self) -> list[np.ndarray]:
        """the (scaled) atom descriptors of the dataset"""
        return self.__V_fs

    @V_fs.setter
    def V_fs(self, V_fs: list[np.ndarray]):
        """the (scaled) atom features of the dataset"""
        self._validate_attribute(V_fs, "atom features")

        self.__V_fs = V_fs
        self._init_cache()

    @property
    def _E_fs(self) -> list[np.ndarray]:
        """the raw bond features of the dataset"""
        return [d.E_f for d in self.data]

    @property
    def E_fs(self) -> list[np.ndarray]:
        """the (scaled) bond features of the dataset"""
        return self.__E_fs

    @E_fs.setter
    def E_fs(self, E_fs: list[np.ndarray]):
        self._validate_attribute(E_fs, "bond features")

        self.__E_fs = E_fs
        self._init_cache()

    @property
    def _V_ds(self) -> list[np.ndarray]:
        """the raw atom descriptors of the dataset"""
        return [d.V_d for d in self.data]

    @property
    def V_ds(self) -> list[np.ndarray]:
        """the (scaled) atom descriptors of the dataset"""
        return self.__V_ds

    @V_ds.setter
    def V_ds(self, V_ds: list[np.ndarray]):
        self._validate_attribute(V_ds, "atom descriptors")

        self.__V_ds = V_ds

    @property
    def d_vf(self) -> int:
        """the extra atom feature dimension, if any"""
        return 0 if self.V_fs[0] is None else self.V_fs[0].shape[1]

    @property
    def d_ef(self) -> int:
        """the extra bond feature dimension, if any"""
        return 0 if self.E_fs[0] is None else self.E_fs[0].shape[1]

    @property
    def d_vd(self) -> int:
        """the extra atom descriptor dimension, if any"""
        return 0 if self.V_ds[0] is None else self.V_ds[0].shape[1]

    def normalize_inputs(
        self, key: str = "X_d", scaler: StandardScaler | None = None
    ) -> StandardScaler:
        VALID_KEYS = {"X_d", "V_f", "E_f", "V_d"}

        match key:
            case "X_d":
                X = None if self.d_xd == 0 else self.X_d
            case "V_f":
                X = None if self.d_vf == 0 else np.concatenate(self.V_fs, axis=0)
            case "E_f":
                X = None if self.d_ef == 0 else np.concatenate(self.E_fs, axis=0)
            case "V_d":
                X = None if self.d_vd == 0 else np.concatenate(self.V_ds, axis=0)
            case _:
                raise ValueError(f"Invalid feature key! got: {key}. expected one of: {VALID_KEYS}")

        if X is None:
            return scaler

        if scaler is None:
            scaler = StandardScaler().fit(X)

        match key:
            case "X_d":
                self.X_d = scaler.transform(X)
            case "V_f":
                self.V_fs = [scaler.transform(V_f) if V_f.size > 0 else V_f for V_f in self.V_fs]
            case "E_f":
                self.E_fs = [scaler.transform(E_f) if E_f.size > 0 else E_f for E_f in self.E_fs]
            case "V_d":
                self.V_ds = [scaler.transform(V_d) if V_d.size > 0 else V_d for V_d in self.V_ds]
            case _:
                raise RuntimeError("unreachable code reached!")

        return scaler

    def reset(self):
        """Reset the atom and bond features; atom and extra descriptors; and targets of each
        datapoint to their initial, unnormalized values."""
        super().reset()
        self.__V_fs = self._V_fs
        self.__E_fs = self._E_fs
        self.__V_ds = self._V_ds


@dataclass
class MolAtomBondDataset(MoleculeDataset, MolAtomBondGraphDataset):
    data: list[MolAtomBondDatapoint]

    def __getitem__(self, idx: int) -> MolAtomBondDatum:
        d = self.data[idx]
        mg = self.mg_cache[idx]

        return MolAtomBondDatum(
            mg,
            self.V_ds[idx],
            self.E_ds[idx],
            self.X_d[idx],
            [
                self.Y[idx] if isinstance(self.Y[idx], np.ndarray) else None,
                self.atom_Y[idx] if self.atom_Y is not None else None,
                self.bond_Y[idx] if self.bond_Y is not None else None,
            ],
            d.weight,
            [d.lt_mask, d.atom_lt_mask, d.bond_lt_mask],
            [d.gt_mask, d.atom_gt_mask, d.bond_gt_mask],
            [d.atom_constraint, d.bond_constraint],
        )

    @property
    def _atom_Y(self) -> list[np.ndarray]:
        """the raw atom targets of the dataset"""
        return [d.atom_y for d in self.data]

    @property
    def atom_Y(self) -> list[np.ndarray]:
        """the (scaled) atom targets of the dataset"""
        return self.__atom_Y

    @atom_Y.setter
    def atom_Y(self, atom_Y: list[np.ndarray]):
        self._validate_attribute(atom_Y, "atom targets")

        self.__atom_Y = atom_Y

    @cached_property
    def _atom_constraints(self) -> np.ndarray:
        return np.array([d.atom_constraint for d in self.data])

    @property
    def atom_constraints(self) -> np.ndarray:
        return self.__atom_constraints

    @atom_constraints.setter
    def atom_constraints(self, atom_constraints: ArrayLike):
        self._validate_attribute(atom_constraints, "atom constraints")
        self.__atom_constraints = np.array(atom_constraints)

    @property
    def _bond_Y(self) -> list[np.ndarray]:
        """the raw bond targets of the dataset"""
        return [d.bond_y for d in self.data]

    @property
    def bond_Y(self) -> list[np.ndarray]:
        """the (scaled) bond targets of the dataset"""
        return self.__bond_Y

    @bond_Y.setter
    def bond_Y(self, bond_Y: list[np.ndarray]):
        self._validate_attribute(bond_Y, "bond targets")

        self.__bond_Y = bond_Y

    @cached_property
    def _bond_constraints(self) -> np.ndarray:
        return np.array([d.bond_constraint for d in self.data])

    @property
    def bond_constraints(self) -> np.ndarray:
        return self.__bond_constraints

    @bond_constraints.setter
    def bond_constraints(self, bond_constraints: ArrayLike):
        self._validate_attribute(bond_constraints, "bond constraints")
        self.__bond_constraints = np.array(bond_constraints)

    @property
    def atom_gt_mask(self) -> np.ndarray:
        return np.vstack([d.atom_gt_mask for d in self.data])

    @property
    def atom_lt_mask(self) -> np.ndarray:
        return np.vstack([d.atom_lt_mask for d in self.data])

    @property
    def bond_gt_mask(self) -> np.ndarray:
        return np.vstack([d.bond_gt_mask for d in self.data])

    @property
    def bond_lt_mask(self) -> np.ndarray:
        return np.vstack([d.bond_lt_mask for d in self.data])

    @property
    def _E_ds(self) -> list[np.ndarray]:
        """the raw bond descriptors of the dataset"""
        return [d.E_d for d in self.data]

    @property
    def E_ds(self) -> list[np.ndarray]:
        """the (scaled) bond descriptors of the dataset"""
        return self.__E_ds

    @E_ds.setter
    def E_ds(self, E_ds: list[np.ndarray]):
        self._validate_attribute(E_ds, "bond descriptors")

        self.__E_ds = E_ds

    @property
    def d_ed(self) -> int:
        """the extra bond descriptor dimension, if any"""
        return 0 if self.E_ds[0] is None else self.E_ds[0].shape[1]

    def normalize_targets(
        self, key: str = "mol", scaler: StandardScaler | None = None
    ) -> StandardScaler:
        VALID_KEYS = {"mol", "atom", "bond"}

        match key:
            case "mol":
                X = self._Y
            case "atom":
                X = np.concatenate(self._atom_Y, axis=0)
            case "bond":
                X = np.concatenate(self._bond_Y, axis=0)
            case _:
                raise ValueError(f"Invalid feature key! got: {key}. expected one of: {VALID_KEYS}")

        if scaler is None:
            scaler = StandardScaler().fit(X)

        match key:
            case "mol":
                self.Y = scaler.transform(X)
            case "atom":
                self.atom_Y = [scaler.transform(y) if y.size > 0 else y for y in self._atom_Y]
                if self.atom_constraints[0] is not None:
                    atoms_per_mol = [len(d.atom_y) for d in self.data]
                    scaled_atom_constraints = [
                        (row - n * scaler.mean_) / scaler.scale_
                        for row, n in zip(self._atom_constraints, atoms_per_mol)
                    ]
                    self.atom_constraints = np.array(scaled_atom_constraints)
            case "bond":
                self.bond_Y = [scaler.transform(y) if y.size > 0 else y for y in self._bond_Y]
                if self.bond_constraints[0] is not None:
                    bonds_per_mol = [len(d.bond_y) for d in self.data]
                    scaled_bond_constraints = [
                        (row - n * scaler.mean_) / scaler.scale_
                        for row, n in zip(self._bond_constraints, bonds_per_mol)
                    ]
                    self.bond_constraints = np.array(scaled_bond_constraints)
            case _:
                raise RuntimeError("unreachable code reached!")

        return scaler

    def normalize_inputs(
        self, key: str = "X_d", scaler: StandardScaler | None = None
    ) -> StandardScaler:
        VALID_KEYS = {"X_d", "V_f", "E_f", "V_d", "E_d"}

        match key:
            case "X_d":
                X = None if self.d_xd == 0 else self.X_d
            case "V_f":
                X = None if self.d_vf == 0 else np.concatenate(self.V_fs, axis=0)
            case "E_f":
                X = None if self.d_ef == 0 else np.concatenate(self.E_fs, axis=0)
            case "V_d":
                X = None if self.d_vd == 0 else np.concatenate(self.V_ds, axis=0)
            case "E_d":
                X = None if self.d_ed == 0 else np.concatenate(self.E_ds, axis=0)
            case _:
                raise ValueError(f"Invalid feature key! got: {key}. expected one of: {VALID_KEYS}")

        if X is None:
            return scaler

        if scaler is None:
            scaler = StandardScaler().fit(X)

        match key:
            case "X_d":
                self.X_d = scaler.transform(X)
            case "V_f":
                self.V_fs = [scaler.transform(V_f) if V_f.size > 0 else V_f for V_f in self.V_fs]
            case "E_f":
                self.E_fs = [scaler.transform(E_f) if E_f.size > 0 else E_f for E_f in self.E_fs]
            case "V_d":
                self.V_ds = [scaler.transform(V_d) if V_d.size > 0 else V_d for V_d in self.V_ds]
            case "E_d":
                self.E_ds = [scaler.transform(E_d) if E_d.size > 0 else E_d for E_d in self.E_ds]
            case _:
                raise RuntimeError("unreachable code reached!")

        return scaler

    def reset(self):
        """Reset the atom and bond features; atom and extra descriptors; and targets of each
        datapoint to their initial, unnormalized values."""
        super().reset()
        self.__E_ds = self._E_ds
        self.__atom_Y = self._atom_Y
        self.__bond_Y = self._bond_Y
        self.__atom_constraints = self._atom_constraints
        self.__bond_constraints = self._bond_constraints


@dataclass
class ReactionDataset(_MolGraphDatasetMixin, MolGraphDataset):
    """A :class:`ReactionDataset` composed of :class:`ReactionDatapoint`\s

    .. note::
        The featurized data provided by this class may be cached, simlar to a
        :class:`MoleculeDataset`. To enable the cache, set ``ReactionDataset
        cache=True``.
    """

    v_attr:np.ndarray
    e_attr:np.ndarray
    e_indices: np.ndarray
    y: np.ndarray
    """the dataset from which to load"""
    # featurizer: Featurizer[Rxn, MolGraph] = field(default_factory=CGRFeaturizer)
    # # """the featurizer with which to generate MolGraphs of the input"""

    def __post_init__(self):
        if self.v_attr is None:
            raise ValueError("Data cannot be None!")

        self.reset()
        self.cache = False

    # @property
    # def cache(self) -> bool:
    #     return self.__cache

    # @cache.setter
    # def cache(self, cache: bool = False):
    #     self.__cache = cache
    #     self.mg_cache = (MolGraphCache if cache else MolGraphCacheOnTheFly)(
    #         self.mols, [None] * len(self), [None] * len(self), self.featurizer
    #     )

    def __getitem__(self, idx: int) -> Datum:
        v_attr = self.v_attr[idx]
        e_attr = self.e_attr[idx]
        e_indices= self.e_indices[idx]
        # y= self.y[idx]
        rev_edge_index = np.arange(len(e_attr)).reshape(-1, 2)[:, ::-1].ravel()
        mg = MolGraph(v_attr, e_attr, e_indices, rev_edge_index)

        return Datum(mg, None, None,self.Y[idx] , 1, None, None)

    # @property
    # def smiles(self) -> list[tuple]:
    #     return [(Chem.MolToSmiles(d.rct), Chem.MolToSmiles(d.pdt)) for d in self.data]

    # @property
    # def mols(self) -> list[Rxn]:
    #     return [(d.rct, d.pdt) for d in self.data]

    # @property
    # def d_vf(self) -> int:
    #     return 0

    # @property
    # def d_ef(self) -> int:
    #     return 0

    # @property
    # def d_vd(self) -> int:
    #     return 0


@dataclass(repr=False, eq=False)
class MulticomponentDataset(_MolGraphDatasetMixin, Dataset):
    """A :class:`MulticomponentDataset` is a :class:`Dataset` composed of parallel
    :class:`MoleculeDatasets` and :class:`ReactionDataset`\s"""

    datasets: list[MoleculeDataset | ReactionDataset]
    """the parallel datasets"""

    def __post_init__(self):
        sizes = [len(dset) for dset in self.datasets]
        if not all(sizes[0] == size for size in sizes[1:]):
            raise ValueError(f"Datasets must have all same length! got: {sizes}")

    def __len__(self) -> int:
        return len(self.datasets[0])

    @property
    def n_components(self) -> int:
        return len(self.datasets)

    def __getitem__(self, idx: int) -> list[Datum]:
        return [dset[idx] for dset in self.datasets]

    @property
    def smiles(self) -> list[list[str]]:
        return list(zip(*[dset.smiles for dset in self.datasets]))

    @property
    def names(self) -> list[list[str]]:
        return list(zip(*[dset.names for dset in self.datasets]))

    @property
    def mols(self) -> list[list[Chem.Mol]]:
        return list(zip(*[dset.mols for dset in self.datasets]))

    def normalize_targets(self, scaler: StandardScaler | None = None) -> StandardScaler:
        return self.datasets[0].normalize_targets(scaler)

    def normalize_inputs(
        self, key: str = "X_d", scaler: list[StandardScaler] | None = None
    ) -> list[StandardScaler]:
        RXN_VALID_KEYS = {"X_d"}
        match scaler:
            case None:
                return [
                    dset.normalize_inputs(key)
                    if isinstance(dset, MoleculeDataset) or key in RXN_VALID_KEYS
                    else None
                    for dset in self.datasets
                ]
            case _:
                assert len(scaler) == len(
                    self.datasets
                ), "Number of scalers must match number of datasets!"

                return [
                    dset.normalize_inputs(key, s)
                    if isinstance(dset, MoleculeDataset) or key in RXN_VALID_KEYS
                    else None
                    for dset, s in zip(self.datasets, scaler)
                ]

    def reset(self):
        return [dset.reset() for dset in self.datasets]

    @property
    def d_xd(self) -> list[int]:
        return self.datasets[0].d_xd

    @property
    def d_vf(self) -> list[int]:
        return sum(dset.d_vf for dset in self.datasets)

    @property
    def d_ef(self) -> list[int]:
        return sum(dset.d_ef for dset in self.datasets)

    @property
    def d_vd(self) -> list[int]:
        return sum(dset.d_vd for dset in self.datasets)
