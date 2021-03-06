# -*- coding: utf-8 -*-
# Copyright (C) Duncan Macleod (2014-2019)
#
# This file is part of GWpy.
#
# GWpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# GWpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GWpy.  If not, see <http://www.gnu.org/licenses/>.

"""Read LIGO_LW documents into :class:`~glue.ligolw.table.Table` objects.
"""

import inspect

import numpy

try:
    from glue.ligolw.lsctables import TableByName
except ImportError:
    TableByName = dict()

from ...io import registry
from ...io.ligolw import (is_ligolw, read_table as read_ligolw_table,
                          write_tables as write_ligolw_tables,
                          patch_ligotimegps,
                          to_table_type as to_ligolw_table_type)
from .. import (Table, EventTable)
from .utils import read_with_selection

__author__ = 'Duncan Macleod <duncan.macleod@ligo.org>'
__all__ = []

# methods to exclude from get_as_columns conversions
GET_AS_EXCLUDE = ['get_column', 'get_table']

# map custom object types to numpy-compatible type
NUMPY_TYPE_MAP = dict()
try:
    from glue.ligolw.lsctables import LIGOTimeGPS
except ImportError:
    pass
else:
    from glue.ligolw.ilwd import ilwdchar
    from glue.ligolw._ilwd import ilwdchar as _ilwdchar
    ilwdchar_types = (ilwdchar, _ilwdchar)
    NUMPY_TYPE_MAP[ilwdchar_types] = numpy.int_
    NUMPY_TYPE_MAP[LIGOTimeGPS] = numpy.float_


# -- utilities ----------------------------------------------------------------

def _get_property_columns(tabletype, columns):
    """Returns list of GPS columns required to read gpsproperties for a table

    Examples
    --------
    >>> _get_property_columns(lsctables.SnglBurstTable, ['peak'])
    ['peak_time', 'peak_time_ns']
    """
    from glue.ligolw.lsctables import gpsproperty as GpsProperty
    # get properties for row object
    rowvars = vars(tabletype.RowType)
    # build list of real column names for fancy properties
    extracols = {}
    for key in columns:
        try:
            prop = rowvars[key]
        except KeyError:
            continue
        if isinstance(prop, GpsProperty):
            extracols[key] = (prop.s_name, prop.ns_name)
    return extracols


# -- conversions --------------------------------------------------------------

def to_astropy_table(llwtable, apytable, copy=False, columns=None,
                     use_numpy_dtypes=False, rename=None):
    """Convert a :class:`~glue.ligolw.table.Table` to an `~astropy.tableTable`

    This method is designed as an internal method to be attached to
    :class:`~glue.ligolw.table.Table` objects as `__astropy_table__`.

    Parameters
    ----------
    llwtable : :class:`~glue.ligolw.table.Table`
        the LIGO_LW table to convert from

    apytable : `type`
        `astropy.table.Table` class or subclass

    copy : `bool`, optional
        if `True` copy the input data, otherwise return a reference,
        default: `False`

    columns : `list` of `str`, optional
        the columns to populate, if not given, all columns present in the
        table are mapped

    use_map_dtypes : `bool`, optional
        force column `dtypes

    rename : `dict`, optional
        dict of ('old name', 'new name') pairs to rename columns
        from the original LIGO_LW table

    Returns
    -------
    table : `EventTable`
        a view of the original data
    """
    # set default keywords
    if rename is None:
        rename = {}
    if columns is None:
        columns = llwtable.columnnames

    # get names of get_xxx() methods for this table
    getters = [
        name.split('_', 1)[1] for (name, _) in
        inspect.getmembers(llwtable, predicate=inspect.ismethod) if
        name.startswith('get_') and name not in GET_AS_EXCLUDE and
        name not in llwtable.columnnames]

    # extract columns from LIGO_LW table as astropy.table.Column
    data = []
    for colname in columns:
        # extract using Table.get_<>()
        if colname in getters:
            with patch_ligotimegps():
                arr = getattr(llwtable, 'get_{}'.format(colname))()

        # extract as standard column
        else:
            arr = llwtable.getColumnByName(colname)

        # transform to astropy.table.Column
        copythis = False if colname in getters else copy
        data.append(to_astropy_column(arr, apytable.Column, copy=copythis,
                                      use_numpy_dtype=use_numpy_dtypes,
                                      name=rename.get(colname, colname)))

    # build table and return
    return apytable(data, copy=False, meta={'tablename': str(llwtable.Name)})


def to_astropy_column(llwcol, cls, copy=False, dtype=None,
                      use_numpy_dtype=False, **kwargs):
    """Convert a :class:`~glue.ligolw.table.Column` to `astropy.table.Column`

    Parameters
    -----------
    llwcol : :class:`~glue.ligolw.table.Column`, `numpy.ndarray`, iterable
        the LIGO_LW column to convert, or an iterable

    cls : `~astropy.table.Column`
        the Astropy `~astropy.table.Column` or subclass to convert to

    copy : `bool`, optional
        if `True` copy the input data, otherwise return a reference,
        default: `False`

    dtype : `type`, optional
        the data type to convert to when creating the `~astropy.table.Column`

    use_numpy_dtype : `bool`, optional
        convert object type to numpy dtype, default: `False`, only used
        with ``dtype=None``

    **kwargs
        other keyword arguments are passed to the `~astropy.table.Column`
        creator

    Returns
    -------
    column : `~astropy.table.Column`
        an Astropy version of the given LIGO_LW column
    """
    if dtype is None:  # try and find dtype
        dtype = _get_column_dtype(llwcol)
        if use_numpy_dtype and numpy.dtype(dtype).type is numpy.object_:
            # dtype maps to 'object' in numpy, try and resolve real numpy type
            try:
                dtype = NUMPY_TYPE_MAP[dtype]
            except KeyError:
                # try subclass matches (mainly for ilwdchar)
                for key in NUMPY_TYPE_MAP:
                    if issubclass(dtype, key):
                        dtype = NUMPY_TYPE_MAP[key]
                        break
                else:  # no subclass matches, raise
                    raise TypeError("no mapping from object type %r to numpy "
                                    "type" % dtype)
    try:
        return cls(data=llwcol, copy=copy, dtype=dtype, **kwargs)
    except TypeError:
        # numpy tries to cast ilwdchar to int via long, which breaks
        if dtype is numpy.int_ and isinstance(llwcol[0], ilwdchar_types):
            return cls(data=map(dtype, llwcol),
                       copy=False, dtype=dtype, **kwargs)
        # any other error, raise
        raise


def _get_column_dtype(llwcol):
    """Get the data type of a LIGO_LW `Column`

    Parameters
    ----------
    llwcol : :class:`~glue.ligolw.table.Column`, `numpy.ndarray`, iterable
        a LIGO_LW column, a numpy array, or an iterable

    Returns
    -------
    dtype : `type`, None
        the object data type for values in the given column, `None` is
        returned if ``llwcol`` is a `numpy.ndarray` with `numpy.object_`
        dtype, or no data type can be parsed (e.g. empty list)
    """
    try:  # maybe its a numpy array already!
        dtype = llwcol.dtype
        if dtype is numpy.dtype('O'):  # don't convert
            raise AttributeError
        return dtype
    except AttributeError:  # dang
        try:  # glue.ligolw.table.Column
            llwtype = llwcol.parentNode.validcolumns[llwcol.Name]
        except AttributeError:  # not a column
            try:
                return type(llwcol[0])
            except IndexError:
                return None
        else:  # map column type str to python type
            from glue.ligolw.types import (ToPyType, ToNumPyType)
            try:
                return ToNumPyType[llwtype]
            except KeyError:
                return ToPyType[llwtype]


def table_to_ligolw(table, tablename):
    """Convert a `astropy.table.Table` to a :class:`glue.ligolw.table.Table`
    """
    from glue.ligolw import lsctables

    # create new LIGO_LW table
    columns = table.columns.keys()
    cls = lsctables.TableByName[tablename]
    llwcolumns = list(columns)
    for col, llwcols in _get_property_columns(cls, columns).items():
        idx = llwcolumns.index(col)
        llwcolumns.pop(idx)
        for name in llwcols[::-1]:
            llwcolumns.insert(idx, name)
    llwtable = lsctables.New(cls, columns=llwcolumns)

    # map rows across
    for row in table:
        llwrow = llwtable.RowType()
        for name in columns:
            setattr(llwrow, name,
                    to_ligolw_table_type(row[name], llwtable, name))
        llwtable.append(llwrow)

    return llwtable


# -- read ---------------------------------------------------------------------

@read_with_selection
def read_table(source, tablename=None, **kwargs):
    """Read a `Table` from one or more LIGO_LW XML documents

    source : `file`, `str`, :class:`~glue.ligolw.ligolw.Document`, `list`
        one or more open files, file paths, or LIGO_LW `Document` objects

    tablename : `str`, optional
        the `Name` of the relevant `Table` to read, if not given a table will
        be returned if only one exists in the document(s)

    **kwargs
        keyword arguments for the read, or conversion functions

    See Also
    --------
    gwpy.io.ligolw.read_table
        for details of keyword arguments for the read operation
    gwpy.table.io.ligolw.to_astropy_table
        for details of keyword arguments for the conversion operation
    """
    from glue.ligolw import table as ligolw_table

    # -- keyword handling -----------------------

    # separate keywords for reading and converting from LIGO_LW to Astropy
    read_kw = kwargs  # rename for readability
    convert_kw = {
        'rename': None,
        'use_numpy_dtypes': False,
    }
    for key in filter(kwargs.__contains__, convert_kw):
        convert_kw[key] = kwargs.pop(key)
    if convert_kw['rename'] is None:
        convert_kw['rename'] = {}

    # allow user to specify LIGO_LW columns to read to provide the
    # desired output columns
    try:
        columns = list(kwargs.pop('columns'))
    except KeyError:
        columns = None
    try:
        read_kw['columns'] = list(kwargs.pop('ligolw_columns'))
    except KeyError:
        read_kw['columns'] = columns
    convert_kw['columns'] = columns or read_kw['columns']

    if tablename:
        tableclass = TableByName[ligolw_table.Table.TableName(tablename)]
        # work out if fancy property columns are required
        #     means 'peak_time' and 'peak_time_ns' will get read if 'peak'
        #     is requested
        if convert_kw['columns'] is not None:
            readcols = set(read_kw['columns'])
            propcols = _get_property_columns(tableclass, convert_kw['columns'])
            for col in propcols:
                try:
                    readcols.remove(col)
                except KeyError:
                    continue
                readcols.update(propcols[col])
            read_kw['columns'] = list(readcols)

    # -- read -----------------------------------

    return Table(read_ligolw_table(source, tablename=tablename, **read_kw),
                 **convert_kw)


# -- write --------------------------------------------------------------------

def write_table(table, target, tablename=None, **kwargs):
    """Write a `~astropy.table.Table` to file in LIGO_LW XML format
    """
    if tablename is None:  # try and get tablename from metadata
        tablename = table.meta.get('tablename', None)
    if tablename is None:  # panic
        raise ValueError("please pass ``tablename=`` to specify the target "
                         "LIGO_LW Table Name")
    return write_ligolw_tables(target, [table_to_ligolw(table, tablename)],
                               **kwargs)


# -- register -----------------------------------------------------------------

for table_ in TableByName.values():
    # register conversion from LIGO_LW to astropy Table
    table_.__astropy_table__ = to_astropy_table

for table_class in (Table, EventTable):
    registry.register_reader('ligolw', table_class, read_table)
    registry.register_writer('ligolw', table_class, write_table)
    registry.register_identifier('ligolw', table_class, is_ligolw)
