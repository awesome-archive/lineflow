from typing import Any, Union, Callable, List, Tuple, Iterator, Iterable
from abc import ABCMeta, abstractmethod
from _collections_abc import _check_methods
import pickle
from pathlib import Path
from itertools import accumulate, chain, islice
from collections import deque
import bisect

import easyfile


class DatasetMixin(metaclass=ABCMeta):

    __slots__ = ()

    def __getitem__(self, index: Union[int, slice]) -> Union[Any, List[Any]]:
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            return [self.get_example(i) for i in range(start, stop, step)]

        if index >= 0:
            if index >= len(self):
                raise IndexError(f'{self.__class__.__name__} object index out of range')
        else:
            if index < - len(self):
                raise IndexError(f'{self.__class__.__name__} object index out of range')
            index += len(self)

        return self.get_example(index)

    @abstractmethod
    def __iter__(self) -> Iterator[Any]:
        while False:
            yield None

    @abstractmethod
    def get_example(self, i: int) -> Any:
        raise IndexError

    @abstractmethod
    def __len__(self) -> int:
        return 0

    @classmethod
    def __subclasshook__(cls, C):
        if cls is DatasetMixin:
            return _check_methods(C, '__iter__', 'get_example', '__len__')
        return NotImplemented


DatasetMixin.register(list)
DatasetMixin.register(str)
DatasetMixin.register(tuple)
DatasetMixin.register(range)
DatasetMixin.register(easyfile.TextFile)
DatasetMixin.register(easyfile.CsvFile)


class Dataset(DatasetMixin):
    def __init__(self,
                 dataset: DatasetMixin) -> None:
        assert isinstance(dataset, DatasetMixin)

        self._dataset = dataset
        self._length = None

    def __iter__(self) -> Iterator[Any]:
        yield from self._dataset

    def get_example(self, i: int) -> Any:
        return self._dataset[i]

    def __len__(self) -> int:
        if self._length is None:
            self._length = len(self._dataset)
        return self._length

    def __add__(self, other: 'Dataset') -> 'ConcatDataset':
        return ConcatDataset(self, other)

    def map(self, map_func: Callable[[Any], Any]) -> 'MapDataset':
        return MapDataset(self, map_func)

    def all(self) -> List[Any]:
        return list(self)

    def take(self, n: int) -> List[Any]:
        return list(islice(self, n))

    def first(self) -> Any:
        return next(iter(self))

    def save(self, filename: str) -> 'CacheDataset':
        path = Path(filename)
        if path.exists():
            print(f'Loading data from {filename}...')
            with path.open('rb') as f:
                cache = pickle.load(f)
        else:
            if not path.parent.exists():
                path.parent.mkdir(parents=True)
            print(f'Saving data to {filename}...')
            cache = list(self)
            with path.open('wb') as f:
                pickle.dump(cache, f)
        return CacheDataset(cache)


class ConcatDataset(Dataset):
    def __init__(self, *datasets: List[DatasetMixin]) -> None:
        assert all(isinstance(d, DatasetMixin) for d in datasets)

        self._datasets = datasets
        self._offsets = None
        self._length = None
        self._ready = False

    def _prepare(self) -> None:
        if self._ready:
            return
        self._lengths = list(accumulate(len(d) for d in self._datasets))
        self._offsets = [0] + self._lengths[:-1]
        self._length = self._lengths[-1]
        self._ready = True

    def __iter__(self) -> Iterator[Any]:
        for d in self._datasets:
            yield from d

    def get_example(self, i: int) -> Any:
        self._prepare()
        j = bisect.bisect_right(self._lengths, i)
        return self._datasets[j][i - self._offsets[j]]

    def __len__(self) -> int:
        self._prepare()
        return self._length


class ZipDataset(Dataset):
    def __init__(self, *datasets: List[DatasetMixin]) -> None:
        assert all(isinstance(d, DatasetMixin) for d in datasets)
        self._datasets = datasets
        self._length = None

    def __iter__(self) -> Iterator[Tuple[Any]]:
        yield from zip(*self._datasets)

    def get_example(self, i: int) -> Tuple[Any]:
        return tuple(d[i] for d in self._datasets)

    def __len__(self) -> int:
        if self._length is None:
            self._length = min(len(d) for d in self._datasets)
        return self._length


class MapDataset(Dataset):
    def __init__(self,
                 dataset: DatasetMixin,
                 map_func: Callable[[Any], Any]) -> None:
        assert callable(map_func)

        self._map_func = map_func

        super(MapDataset, self).__init__(dataset)

    def __iter__(self) -> Iterator[Any]:
        yield from map(self._map_func, self._dataset)

    def get_example(self, i: int) -> Any:
        return self._map_func(self._dataset[i])


class CacheDataset(Dataset):
    def __init__(self, cache: List[Any]) -> None:
        super(CacheDataset, self).__init__(cache)

        self._length = len(cache)


def lineflow_concat(*datasets: List[DatasetMixin]) -> ConcatDataset:
    return ConcatDataset(*datasets)


def lineflow_zip(*datasets: List[DatasetMixin]) -> ZipDataset:
    return ZipDataset(*datasets)


def lineflow_filter(
        predicate: Callable[[Any], bool],
        dataset: DatasetMixin,
        lazy: bool = False) -> Union[Iterator[Any], List[Any]]:
    iterator = filter(predicate, dataset)
    if lazy:
        return iterator
    else:
        return list(iterator)


def lineflow_flat_map(
        map_func: Callable[[Iterable[Any]], Any],
        dataset: DatasetMixin,
        lazy: bool = False) -> Union[Iterator[Any], List[Any]]:
    iterator = chain.from_iterable(map(map_func, dataset))
    if lazy:
        return iterator
    else:
        return list(iterator)


def lineflow_window(
        dataset: DatasetMixin,
        window_size: int,
        shift: int = None,
        lazy: bool = False) -> Union[Iterator[Any], List[Any]]:
    shift = shift or window_size

    def generator(dataset, window_size, shift):
        window = deque([], window_size)
        append = window.append

        skip = 0
        for i, x in enumerate(dataset):
            append(x)
            if skip:
                skip -= 1
                continue
            elif len(window) == window_size:
                yield tuple(window)
                skip = shift - 1

        if skip:
            popleft = window.popleft
            skip += 1
            while skip:
                popleft()
                skip -= 1
            if window:
                yield tuple(window)

    iterator = generator(dataset, window_size, shift)
    if lazy:
        return iterator
    else:
        return list(iterator)


def lineflow_load(filename: str) -> Dataset:
    print(f'Loading data from {filename}...')
    with open(filename, 'rb') as f:
        dataset = pickle.load(f)
    return Dataset(dataset)
