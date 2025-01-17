# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members
#
# pylint: disable=C0111

import traceback
import warnings
from importlib.metadata import distributions
from importlib.metadata import entry_points

from zope.interface import Invalid
from zope.interface.verify import verifyClass

from buildbot.errors import PluginDBError
from buildbot.interfaces import IPlugin

# Base namespace for Buildbot specific plugins
_NAMESPACE_BASE = 'buildbot'


def find_distribution_info(entry_point_name, entry_point_group):
    for distribution in distributions():
        # each distribution can have many entry points
        try:
            for ep in distribution.entry_points:
                if ep.name == entry_point_name and ep.group == entry_point_group:
                    return (distribution.metadata['Name'], distribution.metadata['Version'])
        except KeyError as exc:
            raise PluginDBError("Plugin info was found, but it is invalid.") from exc
    raise PluginDBError("Plugin info not found.")


class _PluginEntry:

    def __init__(self, group, entry, loader):
        self._group = group
        self._entry = entry
        self._value = None
        self._loader = loader
        self._load_warnings = []
        self._info = None

    def load(self):
        if self._value is None:
            with warnings.catch_warnings(record=True) as all_warnings:
                warnings.simplefilter("always")
                self._value = self._loader(self._entry)
                self._load_warnings = list(all_warnings)

    @property
    def group(self):
        return self._group

    @property
    def name(self):
        return self._entry.name

    @property
    def info(self):
        if self._info is None:
            self._info = find_distribution_info(self._entry.name, self._group)
        return self._info

    def __eq__(self, other):
        return self.info == other.info

    def __ne__(self, other):
        return not self.__eq__(other)

    @property
    def value(self):
        self.load()
        for w in self._load_warnings:
            warnings.warn_explicit(w.message, w.category, w.filename, w.lineno)
        return self._value


class _PluginEntryProxy(_PluginEntry):

    """Proxy for specific entry with custom group name.

    Used to provided access to the same entry from different namespaces.
    """

    def __init__(self, group, plugin_entry):
        assert isinstance(plugin_entry, _PluginEntry)
        self._plugin_entry = plugin_entry
        self._group = group

    def load(self):
        self._plugin_entry.load()

    @property
    def group(self):
        return self._group

    @property
    def name(self):
        return self._plugin_entry.name

    @property
    def info(self):
        return self._plugin_entry.info

    @property
    def value(self):
        return self._plugin_entry.value


class _NSNode:
    # pylint: disable=W0212

    def __init__(self):
        self._children = {}

    def load(self):
        for child in self._children.values():
            child.load()

    def add(self, name, entry):
        assert isinstance(name, str) and isinstance(entry, _PluginEntry)
        self._add(name, entry)

    def _add(self, name, entry):
        path = name.split('.', 1)
        key = path.pop(0)
        is_leaf = not path
        child = self._children.get(key)

        if is_leaf:
            if child is not None:
                assert isinstance(child, _PluginEntry)
                if child != entry:
                    raise PluginDBError(
                        f'Duplicate entry point for "{child.group}:{child.name}".\n'
                        f'  Previous definition {child.info}\n'
                        f'  This definition {entry.info}')
            else:
                self._children[key] = entry
        else:
            if child is None:
                child = _NSNode()
            assert isinstance(child, _NSNode)
            child._add(path[0], entry)
            self._children[key] = child

    def __getattr__(self, name):
        child = self._children.get(name)
        if child is None:
            raise PluginDBError(f'Unknown component name: {name}')

        if isinstance(child, _PluginEntry):
            return child.value
        return child

    def info(self, name):
        assert isinstance(name, str)

        return self._get(name).info

    def get(self, name):
        assert isinstance(name, str)

        return self._get(name).value

    def _get(self, name):
        path = name.split('.', 1)
        key = path.pop(0)
        is_leaf = not path
        child = self._children.get(key)

        if isinstance(child, _PluginEntry):
            if not is_leaf:
                raise PluginDBError(f'Excessive namespace specification: {path[0]}')
            return child
        elif child is None:
            raise PluginDBError(f'Unknown component name: {name}')
        else:
            return child._get(path[0])

    def _info_all(self):
        result = []
        for key, child in self._children.items():
            if isinstance(child, _PluginEntry):
                result.append((key, child.info))
            else:
                result.extend([
                    (f'{key}.{name}', value)
                    for name, value in child.info_all().items()
                ])
        return result

    def info_all(self):
        return dict(self._info_all())


class _Plugins:

    """
    represent plugins within a namespace
    """

    def __init__(self, namespace, interface=None):
        if interface is not None:
            assert interface.isOrExtends(IPlugin)

        self._group = f'{_NAMESPACE_BASE}.{namespace}'

        self._interface = interface
        self._real_tree = None

    def _load_entry(self, entry):
        # pylint: disable=W0703
        try:
            result = entry.load()
        except Exception as e:
            # log full traceback of the bad entry to help support
            traceback.print_exc()
            raise PluginDBError(f'Unable to load {self._group}:{entry.name}: {str(e)}') from e
        if self._interface:
            try:
                verifyClass(self._interface, result)
            except Invalid as e:
                raise PluginDBError(
                    f'Plugin {self._group}:{entry.name} does not implement '
                    f'{self._interface.__name__}: {str(e)}') from e
        return result

    @property
    def _tree(self):
        if self._real_tree is None:
            self._real_tree = _NSNode()
            entries = entry_points().get(self._group, [])
            for entry in entries:
                self._real_tree.add(entry.name,
                                    _PluginEntry(self._group, entry,
                                                 self._load_entry))
        return self._real_tree

    def load(self):
        self._tree.load()

    def info_all(self):
        return self._tree.info_all()

    @property
    def names(self):
        # Expensive operation
        return list(self.info_all())

    def info(self, name):
        """
        get information about a particular plugin if known in this namespace
        """
        return self._tree.info(name)

    def __contains__(self, name):
        """
        check if the given name is available as a plugin
        """
        try:
            return not isinstance(self._tree.get(name), _NSNode)
        except PluginDBError:
            return False

    def get(self, name):
        """
        get an instance of the plugin with the given name
        """
        return self._tree.get(name)

    def _get_entry(self, name):
        return self._tree._get(name)

    def __getattr__(self, name):
        try:
            return getattr(self._tree, name)
        except PluginDBError as e:
            raise AttributeError(str(e)) from e


class _PluginDB:

    """
    Plugin infrastructure support for Buildbot
    """

    def __init__(self):
        self._namespaces = {}

    def add_namespace(self, namespace, interface=None, load_now=False):
        """
        register given namespace in global database of plugins

        in case it's already registered, return the registration
        """
        tempo = self._namespaces.get(namespace)

        if tempo is None:
            tempo = _Plugins(namespace, interface)
            self._namespaces[namespace] = tempo

        if load_now:
            tempo.load()

        return tempo

    @property
    def namespaces(self):
        """
        get a list of registered namespaces
        """
        return list(self._namespaces)

    def info(self):
        """
        get information about all plugins in registered namespaces
        """
        result = {}
        for name, namespace in self._namespaces.items():
            result[name] = namespace.info_all()
        return result


_DB = _PluginDB()


def namespaces():
    """
    provide information about known namespaces
    """
    return _DB.namespaces


def info():
    """
    provide information about all known plugins

    format of the output:

    {<namespace>, {
        {<plugin-name>: (<package-name>, <package-version),
         ...},
        ...
    }
    """
    return _DB.info()


def get_plugins(namespace, interface=None, load_now=False):
    """
    helper to get a direct interface to _Plugins
    """
    return _DB.add_namespace(namespace, interface, load_now)
