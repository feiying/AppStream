# Copyright (C) 2011 Canonical
#
# Authors:
#  Alex Eftimie
#  Matthias Klumpp
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; version 3.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA

from gi.repository import PackageKitGlib as packagekit
from gi.repository import GObject
from gi.repository import GLib as glib
from gi.repository import Gio as gio
import logging
import locale

from gettext import gettext as _

from softwarecenter.db.pkginfo import PackageInfo, _Version
from softwarecenter.distro import get_distro

LOG = logging.getLogger('softwarecenter.db.packagekit')

class PkOrigin:
    def __init__(self, repo):
        if repo:
            repo_id = repo.get_property('repo-id')
            if repo_id.endswith('-source'):
                repo_id = repo_id[:-len('-source')]
                self.component = 'source'
            elif repo_id.endswith('-debuginfo'):
                repo_id = repo_id[:-len('-debuginfo')]
                self.component = 'debuginfo'
            else:
                self.component = 'main'

            if repo_id == 'updates':
                self.origin = get_distro().get_distro_channel_name()
                self.archive = 'stable'
            elif repo_id == 'updates-testing':
                self.origin = get_distro().get_distro_channel_name()
                self.archive = 'testing'
            elif repo_id.endswith('-updates-testing'):
                self.origin = repo_id[:-len('-updates-testing')]
                self.archive = 'testing'
            else:
                self.origin = repo_id
                self.archive = 'stable'

            self.trusted = True
            self.label = repo.get_property('description')
        else:
            self.origin = 'unknown'
            self.archive = 'unknown'
            self.trusted = False
            self.label = _("Unknown repository")
            self.component = 'main'
        self.site = ''


class PackagekitVersion(_Version):
    def __init__(self, package, pkginfo):
        self.package = package
        self.pkginfo = pkginfo

    @property
    def description(self):
        pkgid = self.package.get_id()
        return self.pkginfo.get_description(pkgid)

    @property
    def downloadable(self):
        return True  # FIXME: check for an equivalent

    @property
    def summary(self):
        return self.package.get_property('summary')

    @property
    def size(self):
        return self.pkginfo.get_size(self.package.get_name())

    @property
    def installed_size(self):
        """In packagekit, installed_size can be fetched only for installed
        packages, and is stored in the same 'size' property as the package
        size"""
        return self.pkginfo.get_installed_size(self.package.get_name())

    @property
    def version(self):
        return self.package.get_version()

    @property
    def origins(self):
        return self.pkginfo.get_origins(self.package.get_name())


def make_locale_string():
    loc = locale.getlocale(locale.LC_MESSAGES)
    if loc[1]:
        return loc[0] + '.' + loc[1]
    return loc[0]


class PackagekitInfo(PackageInfo):
    USE_CACHE = True

    def __init__(self):
        super(PackagekitInfo, self).__init__()
        self.pktask = packagekit.Task()
        self.pktask.set_locale(make_locale_string())
        self._cache_pkg_filter_none = {} # temporary hack for decent testing
        self._cache_pkg_filter_newest = {} # temporary hack for decent testing
        self._cache_details = {} # temporary hack for decent testing
        self._notfound_cache_pkg = []
        self._repocache = {}
        self._ready = False
        self.distro = get_distro()
        self._pkgs_cache = {}

    def __contains__(self, pkgname):
        # setting it like this for now
        return pkgname not in self._notfound_cache_pkg

    def open(self):
        """ (re)open the cache, this sends cache-invalid, cache-ready signals
        """
        LOG.info("packagekit.cache.open()")
        self.emit("cache-invalid")
        if not self._ready or not self._pkgs_cache:
            self._fill_package_cache(fast = True, force = True)
            # Start async update of package-cache, as the data which was
            # loaded before (in fast-mode) might not be fully up-to-date
            self._update_package_cache_async()

        if self._ready:
            self.emit("cache-ready")

    def _prefill_descriptions_helper(self, batch):
        if not batch:
            return

        ids = [p.get_id() for p in batch]

        try:
            result = self.pktask.get_details(ids, None, self._on_progress_changed, None)
            details = result.get_details_array()
        except GObject.GError as e:
            LOG.info('Cannot get details when prefilling cache cache: %s' % e)
            return

        for detail in details:
            packageid = detail.get_property('package-id')
            self._cache_details[packageid] = detail

    def _add_package_to_cache(self, pkg):
        self._pkgs_cache.update({ pkg.get_name() : pkg })

    def _pkit_getpackages_finished(self, res, replacecache = False):
        try:
            results = self.pktask.generic_finish(res)
        except GObject.GError as e:
            LOG.info('failed to search: %s' % e);
            loop.quit()
            return

        error_code = results.get_error_code();
        if error_code is not None:
            #TODO
            print "Got ERROR!"

        # Clear the cache, then update it with fresh data
        if replacecache:
            self._pkgs_cache = {}

        # get the data
        LOG.info('Adding package data')
        array = results.get_package_array();
        for pkg in array:
            self._add_package_to_cache(pkg)

        # cache is prepared now, we're ready
        self._ready = True

    def _update_package_cache_async(self):
        """ Async function to update the package cache with fresh
        and up-to-date data from PackageKit """

        # we never want source packages
        pfilter = 1 << packagekit.FilterEnum.NOT_SOURCE

        self.pktask.get_packages_async(pfilter, None, lambda prog, t, u: None,
          None, lambda s, r, u: self._pkit_getpackages_finished(r, True), None)


    def _fill_package_cache(self, fast = False, force = False):
        """Build a package-cache, to allow fast searching of packages.
        If fast is false, the cache will be filled with fresh data, but
        loading it might take longer.
        This function is syncronous """

        # we never want source packages
        pfilter = 1 << packagekit.FilterEnum.NOT_SOURCE
        #if only_newest:
         #pfilter |= 1 << packagekit.FilterEnum.NOT_INSTALLED

        if force or not self._pkgs_cache:
            self._pkgs_cache = {}
            self_ready = False
            if fast:
                """ Use PackageKit cache to make loading the package cache faster.
                To get fresh data, we need to run the non-fast mode async later. """
                fp = gio.File.new_for_path("/var/lib/PackageKit/system.package-list")
                if fp.query_exists(None):
                    pksack = packagekit.PackageSack()
                    pksack.add_packages_from_file (fp)
                    array = pksack.get_array()
                    for pkg in array:
                        self._add_package_to_cache(pkg)
                else:
                    self._fill_package_cache(False, force)
                    return
            else:
		import os
		if not os.path.isfile("/var/cache/yum/i386/\$releasever/neokylin/repomd.xml"):
                    self.warnning_network_connecting 
		    return
                res = self.pktask.get_packages_sync(pfilter, None, lambda prog, t, u: None, None)
                array = res.get_package_array();
                for pkg in array:
                    self._add_package_to_cache(pkg)
            if self._pkgs_cache:
                self._ready = True
    def warnning_network_connecting(self):
        import gtk
        md = gtk.MessageDialog(self, gtk.DIALOG_DESTROY_WITH_PARENT, gtk.MESSAGE_INFO, gtk.BUTTONS_CLOSE, _("If you want use software-center, please connencting network, and restart software-center"))
        md.run()
        md.destroy()

    def is_installed(self, pkgname):
        pkgs = self._get_packages(pkgname)
        if pkgs is None:
            return False
        for p in pkgs:
	    if p.get_info() == packagekit.InfoEnum.INSTALLED:
		return True
        return False

    def is_upgradable(self, pkgname):
        # FIXME: how is this done via PK ?
        return False

    def is_available(self, pkgname):
        # FIXME: i don't think this is being used
        return True

    def get_installed(self, pkgname):
        for p in self._get_packages(pkgname):
	    if p.get_info() == packagekit.InfoEnum.INSTALLED:
		return PackagekitVersion(p, self)
        return None

    def get_candidate(self, pkgname):
        p = self._get_one_package(pkgname, pfilter=packagekit.FilterEnum.NEWEST)
        return PackagekitVersion(p, self) if p else None

    def get_versions(self, pkgname):
        return [PackagekitVersion(p, self) for p in self._get_packages(pkgname)]

    def get_section(self, pkgname):
        # FIXME: things are fuzzy here - group-section association
        p = self._get_one_package(pkgname)
        if p:
            return packagekit.group_enum_to_string(p.get_property('group'))

    def get_summary(self, pkgname):
        p = self._get_one_package(pkgname)
        return p.get_property('summary') if p else ''

    def get_description(self, packageid):
        p = self._get_package_details(packageid)
        return p.get_property('description').replace('\n', ' ') if p else ''

    def get_website(self, pkgname):
        p = self._get_one_package(pkgname)
        if not p:
            return ''
        p = self._get_package_details(p.get_id())
        return p.get_property('url') if p else ''

    def get_installed_files(self, pkgname):
        p = self._get_one_package(pkgname)
        if not p:
            return []
        res = self.pktask.get_files((p.get_id(),), None, self._on_progress_changed, None)
        files = res.get_files_array()
        if not files:
            return []
        return files[0].get_property('files')

    def get_size(self, pkgname):
        p = self._get_one_package(pkgname)
        if not p:
            return -1
        p = self._get_package_details(p.get_id())
        return p.get_property('size') if p else -1

    def get_installed_size(self, pkgname):
        return self.get_size(pkgname)

    def get_origins(self, pkgname, cache=USE_CACHE):
        self._get_repolist()
        # FIXME: this is wrong, as cache is not about NOT_INSTALLED; and if we
        # don't use the cache, we use NOT_INSTALLED (which is possibly wrong
        # too?)
        if cache and (pkgname in self._cache_pkg_filter_none.keys()):
            pkgs = self._cache_pkg_filter_none[pkgname]
        else:
            pkgs = self._get_packages(pkgname, pfilter=packagekit.FilterEnum.NOT_INSTALLED)

        out = set()

        for p in pkgs:
            repoid = p.get_data()
            try:
                out.add(PkOrigin(self._repocache[repoid]))
            except KeyError:
                # could be a removed repository
                LOG.info('key %s not found in repocache' % repoid)
                out.add(PkOrigin(None))

        return out

    def get_origin(self, pkgname):
        p = self._get_one_package(pkgname)
        if not p:
            return ''
        origin = p.get_data()
        if origin.startswith('installed:'):
            return origin[len('installed:'):]
        return origin

    def component_available(self, distro_codename, component):
        # FIXME stub
        return True

    def get_addons(self, pkgname, ignore_installed=True):
        # FIXME implement it
        return ([], [])

    def get_packages_removed_on_remove(self, pkg):
        """ Returns a package names list of reverse dependencies
        which will be removed if the package is removed."""
        p = self.get_installed(pkg.name)
        if not p:
            return []
        autoremove = False
        res = self.pktask.simulate_remove_packages((p.package.get_id(),),
                                            autoremove, None,
                                            self._on_progress_changed, None,
        )
        if not res:
            return []
        return [p.get_name() for p in res.get_package_array() if p.get_name() != pkg.name]

    def get_packages_removed_on_install(self, pkg):
        """ Returns a package names list of dependencies
        which will be removed if the package is installed."""
        p = self.get_candidate(pkg.name)
        if not p:
            return []
        res = self.pktask.simulate_install_packages((p.package.get_id(),),
                                            None,
                                            self._on_progress_changed, None,
        )
        if not res:
            return []
        return [p.get_name() for p in res.get_package_array() if (p.get_name() != pkg.name) and p.get_info() == packagekit.InfoEnum.INSTALLED]

    def get_total_size_on_install(self, pkgname,
                                  addons_install=None, addons_remove=None,
                                  archive_suite=None):
        """ Returns a tuple (download_size, installed_size)
        with disk size in KB calculated for pkgname installation
        plus addons change.
        """
        # FIXME: support archive_suite here too

        # FIXME: PackageKit reports only one size at a time
        if self.is_installed(pkgname):
            return (0, self.get_size(pkgname))
        else:
            return (self.get_size(pkgname), 0)

    @property
    def ready(self):
        """ No PK equivalent, simply returning True """
        return True

    def get_license(self, pkgname):
        p = self._get_one_package(pkgname)
        if not p:
            return ""
        details = self._get_package_details(p.get_property('package-id'))
        if not details:
            return ""
        return details.get_property('license')

    """ private methods """
    def _get_package_details(self, packageid, cache=USE_CACHE):
        LOG.debug("package_details %s", packageid) #, self._cache.keys()
        if cache and (packageid in self._cache_details.keys()):
            return self._cache_details[packageid]

        try:
            result = self.pktask.get_details((packageid,), None, self._on_progress_changed, None)
        except GObject.GError as e:
            return None

        pkgs = result.get_details_array()
        if not pkgs:
            return None
        packageid = pkgs[0].get_property('package-id')
        self._cache_details[packageid] = pkgs[0]
        return pkgs[0]

    def _get_one_package(self, pkgname, pfilter=packagekit.FilterEnum.NONE, cache=USE_CACHE):
        LOG.debug("package_one %s", pkgname) #, self._cache.keys()
        ps = self._get_packages(pkgname, pfilter)
        if not ps:
            # also keep it in not found, to prevent further calls of resolve
            if pkgname not in self._notfound_cache_pkg:
                LOG.debug("blacklisted %s", pkgname)
                self._notfound_cache_pkg.append(pkgname)
            return None

        return ps[0]

    def _get_packages(self, pkgname, pfilter=packagekit.FilterEnum.NONE):
        """ make sure we're ready and have a working cache """
        if not self._ready:
            return None

        """ resolve a package name into a PkPackage object or return None """
        LOG.debug("fetch packages for %s", pkgname) #, self._cache.keys()

        if pfilter in (packagekit.FilterEnum.NONE, packagekit.FilterEnum.NOT_SOURCE):
            cache_pkg_filter = self._cache_pkg_filter_none
        elif pfilter in (packagekit.FilterEnum.NEWEST,):
            cache_pkg_filter = self._cache_pkg_filter_newest
        else:
            cache_pkg_filter = None

#        if cache and cache_pkg_filter is not None and (pkgname in cache_pkg_filter.keys()):
#            return cache_pkg_filter[pkgname]

        pfilter = 1 << pfilter
        # we never want source packages
        pfilter |= 1 << packagekit.FilterEnum.NOT_SOURCE

        pkgs = []
        if pkgname in self._pkgs_cache:
	    pkgs.append(self._pkgs_cache[pkgname])
	if pkgs:
	    LOG.debug('Found package: %s' % pkgname)

        #print "Package: %s", self._pkgs_cache[pkgname]

        return pkgs

    def _get_repolist(self, pfilter=packagekit.FilterEnum.NONE, cache=USE_CACHE):
        """ obtain and cache a dictionary of repositories """
        if self._repocache:
            return self._repocache

        pfilter = 1 << pfilter
        result = self.pktask.get_repo_list(pfilter,
                                           None,
                                           self._on_progress_changed, None)
        repos = result.get_repo_detail_array()
        for r in repos:
            self._repocache[r.get_property('repo-id')] = r

    def _reset_cache(self, name=None):
        # fixed package status error when _pkgs_cache not refresh.
        pfilter = packagekit.FilterEnum.NONE
        pfilter = 1 << pfilter
        # we never want source packages
        pfilter |= 1 << packagekit.FilterEnum.NOT_SOURCE

        pkgs = []
        result = self.pktask.resolve(pfilter, (name,), None, self._on_progress_changed, None)
        pkgs = result.get_package_array()
        for pkg in pkgs:
            if pkg.get_name() == name:
                self._pkgs_cache.update({name:pkg})

        # cache is prepared now, we're ready
        self._ready = True

        # Clean resolved packages cache
        # This is used after finishing a transaction, so that we always
        # have the latest package information
        LOG.debug("[reset_cache] name: %s", name)
        if name and (name in self._cache_pkg_filter_none.keys()):
            del self._cache_pkg_filter_none[name]
        if name and (name in self._cache_pkg_filter_newest.keys()):
            del self._cache_pkg_filter_newest[name]
        if name and (name in self._cache_details.keys()):
            del self._cache_details[name]
        else:
            # delete all
            self._cache_pkg_filter_none = {}
            self._cache_pkg_filter_newest = {}
            self._cache_details = {}
        # appdetails gets refreshed:
        self.emit('cache-ready')

    def _on_progress_changed(self, progress, ptype, data=None):
        pass

if __name__ == "__main__":
   pi = PackagekitInfo()

   print "Firefox, installed ", pi.is_installed('firefox')
