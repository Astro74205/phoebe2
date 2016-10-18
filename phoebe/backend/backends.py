
import numpy as np
import commands

from phoebe.parameters import dataset as _dataset
from phoebe.parameters import ParameterSet
from phoebe import dynamics
from phoebe.backend import universe, etvs, horizon_analytic
from phoebe.distortions  import roche
from phoebe.frontend import io
from phoebe import u, c
from phoebe import conf

try:
    import phoebeBackend as phb1
except ImportError:
    _use_phb1 = False
else:
    _use_phb1 = True

import logging
logger = logging.getLogger("BACKENDS")
logger.addHandler(logging.NullHandler())


# protomesh is the mesh at periastron in the reference frame of each individual star
_backends_that_support_protomesh = ['phoebe', 'legacy']
# automesh is meshes with filled observable columns (fluxes etc) at each point at which the mesh is used
_backends_that_support_automesh = ['phoebe', 'legacy']
# the following list is for backends that use numerical meshes
_backends_that_require_meshing = ['phoebe', 'legacy']

def _needs_mesh(b, dataset, kind, component, compute):
    """
    """
    # print "*** _needs_mesh", kind
    compute_kind = b.get_compute(compute).kind
    if compute_kind not in _backends_that_require_meshing:
        # then we don't have meshes for this backend, so all should be False
        return False

    if kind not in ['mesh', 'lc', 'rv']:
        return False

    if kind == 'lc' and compute_kind=='phoebe' and b.get_value(qualifier='lc_method', compute=compute, dataset=dataset, context='compute')=='analytical':
        return False

    if kind == 'rv' and (compute_kind == 'legacy' or b.get_value(qualifier='rv_method', compute=compute, component=component, dataset=dataset, context='compute')=='dynamical'):
        return False

    return True


def _timequalifier_by_kind(kind):
    if kind=='etv':
        return 'time_ephems'
    else:
        return 'times'

def _extract_from_bundle_by_time(b, compute, protomesh=False, pbmesh=False, times=None, allow_oversample=False, **kwargs):
    """
    Extract a list of sorted times and the datasets that need to be
    computed at each of those times.  Any backend can then loop through
    these times and see what quantities are needed for that time step.

    Empty copies of synthetics for each applicable dataset are then
    created and returned so that they can be filled by the given backend.
    Setting of other meta-data should be handled by the bundle once
    the backend returns the filled synthetics.

    :parameter b: the :class:`phoebe.frontend.bundle.Bundle`
    :return: times (list of floats), infos (list of lists of dictionaries),
        new_syns (ParameterSet containing all new parameters)
    :raises NotImplementedError: if for some reason there is a problem getting
        a unique match to a dataset (shouldn't ever happen unless
        the user overrides a label)
    """
    provided_times = times
    times = []
    infos = []
    needed_syns = []

    # for each dataset, component pair we need to check if enabled
    for dataset in b.datasets:

        if dataset=='_default':
            continue

        try:
            dataset_enabled = b.get_value(qualifier='enabled', dataset=dataset, compute=compute, context='compute')
        except ValueError: # TODO: custom exception for no parameter
            # then this backend doesn't support this kind
            continue

        if not dataset_enabled:
            # then this dataset is disabled for these compute options
            continue

        for component in b.hierarchy.get_stars()+[None]:
            obs_ps = b.filter(context='dataset', dataset=dataset, component=component).exclude(kind='*_dep')
            # only certain kinds accept a component of None
            if component is None and obs_ps.kind not in ['lc', 'mesh']:
                # TODO: should we change things like lightcurves to be tagged with the component label of the orbit instead of None?
                # anything that can accept observations on the "system" level should accept component is None
                continue

            timequalifier = _timequalifier_by_kind(obs_ps.kind)
            try:
                this_times = obs_ps.get_value(qualifier=timequalifier, component=component, unit=u.d)
            except ValueError: #TODO: custom exception for no parameter
                continue


            #if not len(this_times):
            #    # then override with passed times if available
            #    this_times = time
            if len(this_times) and provided_times is not None:
                # then overrride the dataset times with the passed times
                this_times = provided_times

            # TODO: also copy this logic for _extract_from_bundle_by_dataset?
            if allow_oversample and \
                    obs_ps.kind in ['lc'] and \
                    b.get_value(qualifier='exptime', dataset=dataset, context='dataset') > 0 and \
                    b.get_value(qualifier='fti_method', dataset=dataset, compute=compute, context='compute', **kwargs)=='oversample':

                # Then we need to override the times retrieved from the dataset
                # with the oversampled times.  Later we'll do an average over
                # the exposure.
                # NOTE: here we assume that the dataset times are at mid-exposure,
                # if we want to allow more flexibility, we'll need a parameter
                # that gives this option and different logic for each case.
                exptime = b.get_value(qualifier='exptime', dataset=dataset, context='dataset', unit=u.d)
                exp_oversample = b.get_value(qualifier='fti_oversample', dataset=dataset, compute=compute, context='compute', **kwargs)
                this_times = np.array([np.linspace(t-exptime/2., t+exptime/2., exp_oversample) for t in this_times]).flatten()

            if len(this_times):
                if component is None and obs_ps.kind in ['mesh']:
                    components = b.hierarchy.get_meshables()
                else:
                    components = [component]

                for component in components:

                    this_info = {'dataset': dataset,
                            'component': component,
                            'kind': obs_ps.kind,
                            'needs_mesh': _needs_mesh(b, dataset, obs_ps.kind, component, compute),
                            'times': this_times}

                    needed_syns.append(this_info)

                    for time_ in this_times:
                        # print "***", time, this_info
                        # TODO: handle some deltatime allowance here
                        if time_ in times:
                            ind = times.index(time_)
                            infos[ind].append(this_info)
                        else:
                            times.append(time_)
                            infos.append([this_info])


    if protomesh:
        needed_syns, infos = _handle_protomesh(b, compute, needed_syns, infos)

    if pbmesh:
        needed_syns, infos = _handle_automesh(b, compute, needed_syns, infos, times=times)

    if len(times):
        ti = zip(times, infos)
        ti.sort()
        times, infos = zip(*ti)

    return np.array(times), infos, _create_syns(b, needed_syns)

def _extract_from_bundle_by_dataset(b, compute, protomesh=False, pbmesh=False, times=[]):
    """
    Extract a list of enabled dataset from the bundle.

    Empty copies of synthetics for each applicable dataset are then
    created and returned so that they can be filled by the given backend.
    Setting of other meta-data should be handled by the bundle once
    the backend returns the filled synthetics.

    Unlike :func:`_extract_from_bundle_by_time`, this does not sort
    by times and combine datasets that need to be computed at the same
    timestamp.  In general, this function will be used by non-PHOEBE
    backends.

    :parameter b: the :class:`phoebe.frontend.bundle.Bundle`
    :return: times (list of floats), infos (list of lists of dictionaries),
        new_syns (ParameterSet containing all new parameters)
    :raises NotImplementedError: if for some reason there is a problem getting
        a unique match to a dataset (shouldn't ever happen unless
        the user overrides a label)
    """
    provided_times = times
    times = []
    infos = []
    needed_syns = []
    # for each dataset, component pair we need to check if enabled
    for dataset in b.datasets:

        if dataset=='_default':
            continue

        try:
            dataset_enabled = b.get_value(qualifier='enabled', dataset=dataset, compute=compute, context='compute')
        except ValueError: # TODO: custom exception for no parameter
            # then this backend doesn't support this kind
            continue

        if not dataset_enabled:
            # then this dataset is disabled for these compute options
            continue

        for component in b.hierarchy.get_stars()+[None]:
            obs_ps = b.filter(context='dataset', dataset=dataset, component=component).exclude(kind='*_dep')
            # only certain kinds accept a component of None
            if component is None and obs_ps.kind not in ['lc', 'mesh']:
                # TODO: should we change things like lightcurves to be tagged with the component label of the orbit instead of None?
                # anything that can accept observations on the "system" level should accept component is None
                continue

            timequalifier = _timequalifier_by_kind(obs_ps.kind)
            try:
                this_times = obs_ps.get_value(qualifier=timequalifier, component=component, unit=u.d)
            except ValueError: #TODO: custom exception for no parameter
                continue

            # if not len(this_times):
                # then override with passed times if available
                # this_times = times_provided
            if len(this_times) and provided_times is not None:
                # then overrride the dataset times with the passed times
                this_times = provided_times

            if len(this_times):

                if component is None and obs_ps.kind in ['mesh']:
                    components = b.hierarchy.get_meshables()
                else:
                    components = [component]

                for component in components:

                    this_info = {'dataset': dataset,
                            'component': component,
                            'kind': obs_ps.kind,
                            'needs_mesh': _needs_mesh(b, dataset, obs_ps.kind, component, compute),
                            'times': this_times
                            }
                    needed_syns.append(this_info)

                    infos.append([this_info])

    if protomesh:
        needed_syns, infos = _handle_protomesh(b, compute, needed_syns, infos)

    if pbmesh:
        needed_syns, infos = _handle_automesh(b, compute, needed_syns, infos, times=False)


#    print "NEEDED", needed_syns
    return infos, _create_syns(b, needed_syns)

def _handle_protomesh(b, compute, needed_syns, infos):
    """
    helper function for functionality needed in both _extract_from_bundle_by_dataset
    and _extract_from_bundle_by_times
    """
    # now add "datasets" for the "protomesh"
    if b.get_compute(compute).kind in _backends_that_support_protomesh:
        for component in b.hierarchy.get_meshables():
            # then we need the prototype synthetics
            this_info = {'dataset': 'protomesh',
                    'component': component,
                    'kind': 'mesh',
                    'needs_mesh': False,
                    'times': [None]}
            needed_syns.append(this_info)

    return needed_syns, infos

def _handle_automesh(b, compute, needed_syns, infos, times=None):
    """
    helper function for functionality needed in both _extract_from_bundle_by_dataset
    and _extract_from_bundle_by_times
    """
    # now add "datasets" for each timepoint at which needs_mesh is True, if pbmesh
    if b.get_compute(compute).kind in _backends_that_support_automesh:

        # building synthetics for the automesh is a little different.  Here we
        # want to add a single "this_info" for each component, and fill that with
        # the total times from all valid datasets.

        # So first let's build a list of datasets we need to consider when finding
        # the times.  We'll do this by first building a dictionary, to avoid duplicates
        # that could occur for datasets with multiple components

        needs_mesh_infos = {info['dataset']: info for info in needed_syns if info['needs_mesh'] and info['kind'] not in ['mesh']}.values()

        # now let's loop over ALL components first... even if a single component
        # is used in the dataset (ie RVs attached to a single component), we'll
        # still build a synthetic for all components
        # TODO: double check to make sure this is fine with the backend and that
        # the backend still fills these correctly (or are they just empty)?
        for component in b.hierarchy.get_meshables():

            # now let's find a list of all times used in all datasets that require
            # the use of a mesh, avoiding duplicates, and maintaining sort order
            this_times = np.array([])
            for info in needs_mesh_infos:
                this_times = np.append(this_times, info['times'])

            # TODO: there must be a better way to do this
            this_times = np.array(list(set(this_times.tolist())))
            this_times.sort()


            this_info = {'dataset': 'pbmesh',
                'component': component,
                'kind': 'mesh',
                'needs_mesh': True,
                'times': this_times}

            if times:
                for time in this_times:
                    ind = times.index(time)
                    infos[ind].append(this_info)

            needed_syns.append(this_info)

    return needed_syns, infos


def _create_syns(b, needed_syns, protomesh=False, pbmesh=False):
    """
    Create empty synthetics

    :parameter b: the :class:`phoebe.frontend.bundle.Bundle`
    :parameter list needed_syns: list of dictionaries containing kwargs to access
        the dataset (dataset, component, kind)
    :return: :class:`phoebe.parameters.parameters.ParameterSet` of all new parameters
    """

    needs_mesh = {info['dataset']: info['kind'] for info in needed_syns if info['needs_mesh']}

    params = []
    for needed_syn in needed_syns:
        # used to be {}_syn
        syn_kind = '{}'.format(needed_syn['kind'])
        if needed_syn['kind']=='mesh':
            # parameters.dataset.mesh will handle creating the necessary columns
            needed_syn['dataset_fields'] = needs_mesh

        these_params, these_constraints = getattr(_dataset, "{}_syn".format(syn_kind.lower()))(**needed_syn)
        # TODO: do we need to handle constraints?
        these_params = these_params.to_list()
        for param in these_params:
            param._component = needed_syn['component']
            if param._dataset is None:
                # dataset may be set for mesh columns
                param._dataset = needed_syn['dataset']
            param._kind = syn_kind
            # context, model, etc will be handle by the bundle once these are returned

        params += these_params

    return ParameterSet(params)

def phoebe(b, compute, times=[], as_generator=False, **kwargs):
    """
    Run the PHOEBE 2.0 backend.  This is the default built-in backend
    so no other pre-requisites are required.

    When using this backend, please cite
        * TODO: include list of citations

    When using dynamics_method=='nbody', please cite:
        * TODO: include list of citations for reboundx

    Parameters that are used by this backend:

    * Compute:
        * all parameters in :func:`phoebe.parameters.compute.phoebe`
    * Orbit:
        * TOOD: list these
    * Star:
        * TODO: list these
    * lc dataset:
        * TODO: list these

    Values that are filled by this backend:

    * lc:
        * times
        * fluxes
    * rv (dynamical only):
        * times
        * rvs

    This function will almost always be called through the bundle, using
        * :meth:`phoebe.frontend.bundle.Bundle.add_compute`
        * :meth:`phoebe.frontend.bundle.Bundle.run_compute`

    :parameter b: the :class:`phoebe.frontend.bundle.Bundle` containing the system
        and datasets
    :parameter str compute: the label of the computeoptions to use (in the bundle).
        These computeoptions must have a kind of 'phoebe'.
    :parameter **kwargs: any temporary overrides to computeoptions
    :return: a list of new synthetic :class:`phoebe.parameters.parameters.ParameterSet`s
    """

    computeparams = b.get_compute(compute, force_ps=True, check_visible=False)
    hier = b.get_hierarchy()

    starrefs  = hier.get_stars()
    meshablerefs = hier.get_meshables()
    # starorbitrefs = [hier.get_parent_of(star) for star in starrefs]
    # orbitrefs = hier.get_orbits()


    protomesh = computeparams.get_value('protomesh', **kwargs)
    pbmesh = computeparams.get_value('pbmesh', **kwargs)
    do_horizon = computeparams.get_value('horizon', **kwargs)
    if 'protomesh' in kwargs.keys():
        # remove protomesh so that it isn't passed twice in _extract_from_bundle_by_time
        kwargs.pop('protomesh')
    if 'pbmesh' in kwargs.keys():
        # remove protomesh so that it isn't passed twice in _extract_from_bundle_by_time
        kwargs.pop('pbmesh')

    times, infos, new_syns = _extract_from_bundle_by_time(b, compute=compute,
                                                          times=times,
                                                          protomesh=protomesh,
                                                          pbmesh=pbmesh,
                                                          allow_oversample=True,
                                                          **kwargs)

    dynamics_method = computeparams.get_value('dynamics_method', **kwargs)
    ltte = computeparams.get_value('ltte', **kwargs)

    distance = b.get_value(qualifier='distance', context='system', unit=u.m)
    t0 = b.get_value(qualifier='t0', context='system', unit=u.d)

    if len(starrefs)==1 and computeparams.get_value('distortion_method', component=starrefs[0], **kwargs) in ['roche']:
        raise ValueError("distortion_method='{}' not valid for single star".format(computeparams.get_value('distortion_method', component=starrefs[0], **kwargs)))

    if len(meshablerefs) > 1 or hier.get_kind_of(meshablerefs[0])=='envelope':
        if dynamics_method in ['nbody', 'rebound']:
            t0, xs0, ys0, zs0, vxs0, vys0, vzs0, inst_ds0, inst_Fs0, ethetas0, elongans0, eincls0 = dynamics.nbody.dynamics_from_bundle(b, [t0], compute, return_roche_euler=True, **kwargs)
            ts, xs, ys, zs, vxs, vys, vzs, inst_ds, inst_Fs, ethetas, elongans, eincls = dynamics.nbody.dynamics_from_bundle(b, times, compute, return_roche_euler=True, **kwargs)

        elif dynamics_method == 'bs':
            # if distortion_method == 'roche':
                # raise ValueError("distortion_method '{}' not compatible with dynamics_method '{}'".format(distortion_method, dynamics_method))

            # TODO: pass stepsize
            # TODO: pass orbiterror
            # TODO: make sure that this takes systemic velocity and corrects positions and velocities (including ltte effects if enabled)
            t0, xs0, ys0, zs0, vxs0, vys0, vzs0, inst_ds0, inst_Fs0, ethetas0, elongans0, eincls0 = dynamics.nbody.dynamics_from_bundle_bs(b, [t0], compute, return_roche_euler=True, **kwargs)
            # ethetas0, elongans0, eincls0 = None, None, None
            ts, xs, ys, zs, vxs, vys, vzs, inst_ds, inst_Fs, ethetas, elongans, eincls = dynamics.nbody.dynamics_from_bundle_bs(b, times, compute, return_roche_euler=True, **kwargs)
            # ethetas, elongans, eincls = None, None, None


        elif dynamics_method=='keplerian':

            # TODO: make sure that this takes systemic velocity and corrects positions and velocities (including ltte effects if enabled)
            t0, xs0, ys0, zs0, vxs0, vys0, vzs0, ethetas0, elongans0, eincls0 = dynamics.keplerian.dynamics_from_bundle(b, [t0], compute, return_euler=True, **kwargs)
            ts, xs, ys, zs, vxs, vys, vzs, ethetas, elongans, eincls = dynamics.keplerian.dynamics_from_bundle(b, times, compute, return_euler=True, **kwargs)

        else:
            raise NotImplementedError

    # TODO: automatically guess body type for each case... based on things like whether the stars are aligned
    # TODO: handle different distortion_methods
    # TODO: skip initializing system if we NEVER need meshes
    system = universe.System.from_bundle(b, compute, datasets=b.datasets, **kwargs)


    # We need to create the mesh at periastron for any of the following reasons:
    # - protomesh
    # - volume-conservation for eccentric orbits
    # We'll assume that this is always done - so even for circular orbits, the initial mesh will just be a scaled version of this mesh
    system.initialize_meshes()

    # Now we should store the protomesh
    if protomesh:
        for component in meshablerefs:
            body = system.get_body(component)

            pmesh = body.get_standard_mesh(scaled=False)  # TODO: provide theta=0.0 when supported
            body._compute_instantaneous_quantities([], [], [], d=1-body.ecc)
            body._fill_loggs(mesh=pmesh)
            body._fill_gravs(mesh=pmesh)
            body._fill_teffs(mesh=pmesh)

            this_syn = new_syns.filter(component=component, dataset='protomesh')

            this_syn['xs'] = pmesh.centers[:,0]# * u.solRad
            this_syn['ys'] = pmesh.centers[:,1]# * u.solRad
            this_syn['zs'] = pmesh.centers[:,2]# * u.solRad
            this_syn['vertices'] = pmesh.vertices_per_triangle
            this_syn['areas'] = pmesh.areas # * u.solRad**2
            this_syn['tareas'] = pmesh.tareas # * u.solRad**2
            this_syn['normals'] = pmesh.tnormals
            this_syn['nxs'] = pmesh.tnormals[:,0]
            this_syn['nys'] = pmesh.tnormals[:,1]
            this_syn['nzs'] = pmesh.tnormals[:,2]

            this_syn['loggs'] = pmesh.loggs.centers
            this_syn['teffs'] = pmesh.teffs.centers
            # this_syn['mu'] = pmesh.mus  # mus aren't filled until placed in orbit

            # NOTE: this is a computed column, meaning the 'r' is not the radius to centers, but rather the
            # radius at which computables have been determined.  This way r should not suffer from a course
            # grid (so much).  Same goes for cosbeta below.
            this_syn['rs'] = pmesh.rs.centers
            # NOTE: no r_proj for protomeshs since we don't have LOS information

            # TODO: need to test the new (ComputedColumn) version of this
            this_syn['cosbetas'] = pmesh.cosbetas.centers
            # this_syn['cosbeta'] = [np.dot(c,n)/ (np.sqrt((c*c).sum())*np.sqrt((n*n).sum())) for c,n in zip(pmesh.centers, pmesh.tnormals)]


    # Now we need to compute intensities at t0 in order to scale pblums for all future times
    # TODO: only do this if we need the mesh for actual computations
    # TODO: move as much of this pblum logic into mesh.py as possible

    kinds = b.get_dataset().kinds
    if 'lc' in kinds or 'rv' in kinds:  # TODO this needs to be WAY more general
        # we only need to handle pblum_scale if we have a dataset kind which requires
        # intensities
        if len(meshablerefs) > 1 or hier.get_kind_of(meshablerefs[0])=='envelope':
            x0, y0, z0, vx0, vy0, vz0, etheta0, elongan0, eincl0 = dynamics.dynamics_at_i(xs0, ys0, zs0, vxs0, vys0, vzs0, ethetas0, elongans0, eincls0, i=0)
        else:
            x0, y0, z0 = [0.], [0.], [0.]
            vx0, vy0, vz0 = [0.], [0.], [0.]
            # TODO: star needs long_an (yaw?)
            etheta0, elongan0, eincl0 = [0.], [0.], [b.get_value('incl', unit=u.rad)]

        system.update_positions(t0, x0, y0, z0, vx0, vy0, vz0, etheta0, elongan0, eincl0, ignore_effects=True)

        for dataset in b.datasets:
            if dataset == '_default':
                continue

            ds = b.get_dataset(dataset=dataset, kind='*dep')

            if ds.kind is None:
                continue

            kind = ds.kind[:-4]

            #print "***", dataset, kind
            if kind not in ['lc']:
                continue

            for component in ds.components:
                if component=='_default':
                    continue

                system.populate_observables(t0, [kind], [dataset],
                                            ignore_effects=True)

            # now for each component we need to store the scaling factor between
            # absolute and relative intensities
            pblum_copy = {}
            for component in meshablerefs:
                if component=='_default':
                    continue
                pblum_ref = b.get_value(qualifier='pblum_ref', component=component, dataset=dataset, context='dataset')
                if pblum_ref=='self':
                    pblum = b.get_value(qualifier='pblum', component=component, dataset=dataset, context='dataset')
                    ld_func = b.get_value(qualifier='ld_func', component=component, dataset=dataset, context='dataset')
                    ld_coeffs = b.get_value(qualifier='ld_coeffs', component=component, dataset=dataset, context='dataset', check_visible=False)

                    system.get_body(component).compute_pblum_scale(dataset, pblum, ld_func=ld_func, ld_coeffs=ld_coeffs)
                else:
                    # then this component wants to copy the scale from another component
                    # in the system.  We'll just store this now so that we make sure the
                    # component we're copying from has a chance to compute its scale
                    # first.
                    pblum_copy[component] = pblum_ref


            # now let's copy all the scales for those that are just referencing another component
            for comp, comp_copy in pblum_copy.items():
                system.get_body(comp)._pblum_scale[dataset] = system.get_body(comp_copy).get_pblum_scale(dataset)


    # MAIN COMPUTE LOOP
    # the outermost loop will be over times.  infolist will be a list of dictionaries
    # with component, kind, and dataset as keys applicable for that current time.
    for i,time,infolist in zip(range(len(times)),times,infos):
        # Check to see what we might need to do that requires a mesh
        # TOOD: make sure to use the requested distortion_method


        # we need to extract positions, velocities, and euler angles of ALL bodies at THIS TIME (i)
        if len(meshablerefs) > 1 or hier.get_kind_of(meshablerefs[0])=='envelope':
            xi, yi, zi, vxi, vyi, vzi, ethetai, elongani, eincli = dynamics.dynamics_at_i(xs, ys, zs, vxs, vys, vzs, ethetas, elongans, eincls, i=i)
        else:
            xi, yi, zi = [0.], [0.], [0.]
            vxi, vyi, vzi = [0.], [0.], [0.]
            # TODO: star needs long_an (yaw?)

            ethetai, elongani, eincli = [0.], [0.], [b.get_value('incl', component=meshablerefs[0], unit=u.rad)]

        if True in [info['needs_mesh'] for info in infolist]:

            if dynamics_method in ['nbody', 'rebound']:
                di = dynamics.at_i(inst_ds, i)
                Fi = dynamics.at_i(inst_Fs, i)
                # by passing these along to update_positions, volume conservation will
                # handle remeshing the stars
            else:
                # then allow d to be determined from orbit and original sma
                # and F to remain fixed
                di = None
                Fi = None



            # TODO: eventually we can pass instantaneous masses and sma as kwargs if they're time dependent
            # masses = [b.get_value('mass', component=star, context='component', time=time, unit=u.solMass) for star in starrefs]
            # sma = b.get_value('sma', component=starrefs[body.ind_self], context='component', time=time, unit=u.solRad)

            system.update_positions(time, xi, yi, zi, vxi, vyi, vzi, ethetai, elongani, eincli, ds=di, Fs=Fi)

            # Now we need to determine which triangles are visible and handle subdivision
            # NOTE: this should come after populate_observables so that each subdivided triangle
            # will have identical local quantities.  The only downside to this is that we can't
            # make a shortcut and only populate observables at known-visible triangles - but
            # frankly that wouldn't save much time anyways and would then be annoying when
            # inspecting or plotting the mesh
            # NOTE: this has been moved before populate observables now to make use
            # of per-vertex weights which are used to determine the physical quantities
            # (ie teff, logg) that should be used in computing observables (ie intensity)

            expose_horizon =  'mesh' in [info['kind'] for info in infolist] and do_horizon
            horizons = system.handle_eclipses(expose_horizon=expose_horizon)

            # Now we can fill the observables per-triangle.  We'll wait to integrate
            # until we're ready to fill the synthetics
            # print "*** system.populate_observables", [info['kind'] for info in infolist if info['needs_mesh']], [info['dataset'] for info in infolist if info['needs_mesh']]
            # kwargss = [{p.qualifier: p.get_value() for p in b.get_dataset(info['dataset'], component=info['component'], kind='*dep').to_list()+b.get_compute(compute, component=info['component']).to_list()+b.filter(qualifier='passband', dataset=info['dataset'], kind='*dep').to_list()} for info in infolist if info['needs_mesh']]

            system.populate_observables(time,
                    [info['kind'] for info in infolist if info['needs_mesh']],
                    [info['dataset'] for info in infolist if info['needs_mesh']])


        # now let's loop through and fill any synthetics at this time step
        # TODO: make this MPI ready by ditching appends and instead filling with all nans and then filling correct index
        for info in infolist:
            # i, time, info['kind'], info['component'], info['dataset']
            cind = starrefs.index(info['component']) if info['component'] in starrefs else None
            # ts[i], xs[cind][i], ys[cind][i], zs[cind][i], vxs[cind][i], vys[cind][i], vzs[cind][i]
            kind = info['kind']


            if kind in ['mesh', 'sp']:
                # print "*** new_syns", new_syns.twigs
                # print "*** filtering new_syns", info['component'], info['dataset'], kind, time
                # print "*** this_syn.twigs", new_syns.filter(kind=kind, time=time).twigs
                this_syn = new_syns.filter(component=info['component'], dataset=info['dataset'], kind=kind, time=time)
            else:
                # print "*** new_syns", new_syns.twigs
                # print "*** filtering new_syns", info['component'], info['dataset'], kind
                # print "*** this_syn.twigs", new_syns.filter(component=info['component'], dataset=info['dataset'], kind=kind).twigs
                this_syn = new_syns.filter(component=info['component'], dataset=info['dataset'], kind=kind)

            # now check the kind to see what we need to fill
            if kind=='rv':
                ### this_syn['times'].append(time) # time array was set when initializing the syns
                if info['needs_mesh']:
                    # TODO: we have to call get here because twig access will trigger on kind=rv and qualifier=rv
                    # print "***", this_syn.filter(qualifier='rv').twigs, this_syn.filter(qualifier='rv').kinds, this_syn.filter(qualifier='rv').components
                    # if len(this_syn.filter(qualifier='rv').twigs)>1:
                        # print "***2", this_syn.filter(qualifier='rv')[1].kind, this_syn.filter(qualifier='rv')[1].component
                    rv = system.observe(info['dataset'], kind=kind, components=info['component'], distance=distance)['rv']
                    this_syn['rvs'].append(rv*u.solRad/u.d)
                else:
                    # then rv_method == 'dynamical'
                    this_syn['rvs'].append(-1*vzi[cind]*u.solRad/u.d)

            elif kind=='lc':

                # print "***", info['component']
                # print "***", system.observe(info['dataset'], kind=kind, components=info['component'])
                l3 = b.get_value(qualifier='l3', dataset=info['dataset'], context='dataset')
                this_syn['fluxes'].append(system.observe(info['dataset'], kind=kind, components=info['component'], distance=distance, l3=l3)['flux'])

            elif kind=='etv':

                # TODO: add support for other etv kinds (barycentric, robust, others?)
                time_ecl = etvs.crossing(b, info['component'], time, dynamics_method, ltte, tol=computeparams.get_value('etv_tol', u.d, dataset=info['dataset'], component=info['component']))

                this_obs = b.filter(dataset=info['dataset'], component=info['component'], context='dataset')
                this_syn['Ns'].append(this_obs.get_parameter(qualifier='Ns').interp_value(time_ephems=time))  # TODO: there must be a better/cleaner way to do this
                this_syn['time_ephems'].append(time)  # NOTE: no longer under constraint control
                this_syn['time_ecls'].append(time_ecl)
                this_syn['etvs'].append(time_ecl-time)  # NOTE: no longer under constraint control

            elif kind=='ifm':
                observables_ifm = system.observe(info['dataset'], kind=kind, components=info['component'], distance=distance)
                for key in observables_ifm.keys():
                    this_syn[key] = observables_ifm[key]

            elif kind=='orb':
                # ts[i], xs[cind][i], ys[cind][i], zs[cind][i], vxs[cind][i], vys[cind][i], vzs[cind][i]

                ### this_syn['times'].append(ts[i])  # time array was set when initializing the syns
                this_syn['xs'].append(xi[cind])
                this_syn['ys'].append(yi[cind])
                this_syn['zs'].append(zi[cind])
                this_syn['vxs'].append(vxi[cind])
                this_syn['vys'].append(vyi[cind])
                this_syn['vzs'].append(vzi[cind])

            elif kind=='mesh':
                # print "*** info['component']", info['component'], " info['dataset']", info['dataset']
                # print "*** this_syn.twigs", this_syn.twigs
                body = system.get_body(info['component'])

                this_syn['pot'] = body._instantaneous_pot
                this_syn['rpole'] = roche.potential2rpole(body._instantaneous_pot, body.q, body.ecc, body.F, body._scale, component=body.comp_no)
                this_syn['volume'] = body.volume

                # TODO: should x, y, z be computed columns of the vertices???
                # could easily have a read-only property at the ProtoMesh level
                # that returns a ComputedColumn for xs, ys, zs (like rs)
                # (also do same for protomesh)
                this_syn['xs'] = body.mesh.centers[:,0]# * u.solRad
                this_syn['ys'] = body.mesh.centers[:,1]# * u.solRad
                this_syn['zs'] = body.mesh.centers[:,2]# * u.solRad
                this_syn['vxs'] = body.mesh.velocities.centers[:,0] * u.solRad/u.d # TODO: check units!!!
                this_syn['vys'] = body.mesh.velocities.centers[:,1] * u.solRad/u.d
                this_syn['vzs'] = body.mesh.velocities.centers[:,2] * u.solRad/u.d
                this_syn['vertices'] = body.mesh.vertices_per_triangle
                this_syn['areas'] = body.mesh.areas # * u.solRad**2
                # TODO remove this 'normals' vector now that we have nx,ny,nz?
                this_syn['normals'] = body.mesh.tnormals
                this_syn['nxs'] = body.mesh.tnormals[:,0]
                this_syn['nys'] = body.mesh.tnormals[:,1]
                this_syn['nzs'] = body.mesh.tnormals[:,2]
                this_syn['mus'] = body.mesh.mus

                this_syn['loggs'] = body.mesh.loggs.centers
                this_syn['teffs'] = body.mesh.teffs.centers
                # TODO: include abun? (body.mesh.abuns.centers)

                # NOTE: these are computed columns, so are not based on the
                # "center" coordinates provided by x, y, z, etc, but rather are
                # the average value across each triangle.  For this reason,
                # they are less susceptible to a coarse grid.
                this_syn['rs'] = body.mesh.rs.centers
                this_syn['r_projs'] = body.mesh.rprojs.centers

                this_syn['visibilities'] = body.mesh.visibilities

                vcs = np.sum(body.mesh.vertices_per_triangle*body.mesh.weights[:,:,np.newaxis], axis=1)
                for i,vc in enumerate(vcs):
                    if np.all(vc==np.array([0,0,0])):
                        vcs[i] = np.full(3, np.nan)
                this_syn['visible_centroids'] = vcs

                # Eclipse horizon
                if do_horizon and horizons is not None:
                    this_syn['horizon_xs'] = horizons[cind][:,0]
                    this_syn['horizon_ys'] = horizons[cind][:,1]
                    this_syn['horizon_zs'] = horizons[cind][:,2]

                # Analytic horizon
                if do_horizon:
                    if body.distortion_method == 'roche':
                        if body.mesh_method == 'marching':
                            q, F, d, Phi = body._mesh_args
                            scale = body._scale
                            euler = [ethetai[cind], elongani[cind], eincli[cind]]
                            pos = [xi[cind], yi[cind], zi[cind]]
                            ha = horizon_analytic.marching(q, F, d, Phi, scale, euler, pos)
                        elif body.mesh_method == 'wd':
                            scale = body._scale
                            pos = [xi[cind], yi[cind], zi[cind]]
                            ha = horizon_analytic.wd(b, time, scale, pos)
                        else:
                            raise NotImplementedError("analytic horizon not implemented for mesh_method='{}'".format(body.mesh_method))

                        this_syn['horizon_analytic_xs'] = ha['xs']
                        this_syn['horizon_analytic_ys'] = ha['ys']
                        this_syn['horizon_analytic_zs'] = ha['zs']


                # Dataset-dependent quantities
                indeps = {'rv': ['rvs', 'intensities', 'normal_intensities', 'boost_factors'], 'lc': ['intensities', 'normal_intensities', 'boost_factors'], 'ifm': []}
                # if conf.devel:
                indeps['rv'] += ['abs_intensities', 'abs_normal_intensities']
                indeps['lc'] += ['abs_intensities', 'abs_normal_intensities']
                for infomesh in infolist:
                    if infomesh['needs_mesh'] and infomesh['kind'] != 'mesh':
                        new_syns.set_value(qualifier='pblum', time=time, dataset=infomesh['dataset'], component=info['component'], kind='mesh', value=body.compute_luminosity(infomesh['dataset']))

                        for indep in indeps[infomesh['kind']]:
                            key = "{}:{}".format(indep, infomesh['dataset'])
                            # print "***", key, indep, new_syns.qualifiers
                            # print "***", indep, time, infomesh['dataset'], info['component'], 'mesh', new_syns.filter(time=time, kind='mesh').twigs
                            try:
                                new_syns.set_value(qualifier=indep, time=time, dataset=infomesh['dataset'], component=info['component'], kind='mesh', value=body.mesh[key].centers)
                            except ValueError:
                                # print "***", key, indep, info['component'], infomesh['dataset'], new_syns.filter(time=time, dataset=infomesh['dataset'], component=info['component'], kind='mesh').twigs
                                raise ValueError("more than 1 result found: {}".format(",".join(new_syns.filter(qualifier=indep, time=time, dataset=infomesh['dataset'], component=info['component'], kind='mesh').twigs)))


            else:
                raise NotImplementedError("kind {} not yet supported by this backend".format(kind))

        if as_generator:
            # this is mainly used for live-streaming animation support
            yield (new_syns, time)

    if not as_generator:
        yield new_syns


def legacy(b, compute, times=[], **kwargs): #, **kwargs):#(b, compute, **kwargs):

    """
    Use PHOEBE 1.0 (legacy) which is based on the Wilson-Devinney code
    to compute radial velocities and light curves for binary systems
    (>2 stars not supported).  The code is available here:

    http://phoebe-project.org/1.0

    PHOEBE 1.0 and the 'phoebeBackend' python interface must be installed
    and available on the system in order to use this plugin.

    When using this backend, please cite
        * Prsa & Zwitter (2005), ApJ, 628, 426

    Parameters that are used by this backend:

    * Compute:
        * all parameters in :func:`phoebe.parameters.compute.legacy`
    * Orbit:
        * TOOD: list these
    * Star:
        * TODO: list these
    * lc dataset:
        * TODO: list these

    Values that are filled by this backend:

    * lc:
        * times
        * fluxes
    * rv (dynamical only):
        * times
        * rvs

    This function will almost always be called through the bundle, using
        * :meth:`phoebe.frontend.bundle.Bundle.add_compute`
        * :meth:`phoebe.frontend.bundle.Bundle.run_compute`

    :parameter b: the :class:`phoebe.frontend.bundle.Bundle` containing the system
        and datasets
    :parameter str compute: the label of the computeoptions to use (in the bundle).
        These computeoptions must have a kind of 'legacy'.
    :parameter **kwargs: any temporary overrides to computeoptions
    :return: a list of new synthetic :class:`phoebe.parameters.parameters.ParameterSet`s

    """


    """

    build up keys for each phoebe1 parameter, so that they correspond
    to the correct phoebe2 parameter. Since many values of x, y, and z
    it is unecessary to make specific dictionary items for them. This
    function does this and also makes sure the correct component is
    being called.

    Args:
        key: The root of the key word used in phoebe1: component: the
        name of the phoebe2 component associated with the given mesh.

    Returns:
        new key(s) which are component specific and vector direction
        specific (xs, ys, zs) for phoebe1 and phoebe2.

    Raises:
        ImportError if the python 'phoebeBackend' interface to PHOEBE
        legacy is not found/installed



    """
    p2to1 = {'tloc':'teffs', 'glog':'loggs', 'vcx':'xs', 'vcy':'ys', 'vcz':'zs', 'grx':'nxs', 'gry':'nys', 'grz':'nzs', 'csbt':'cosbetas', 'rad':'rs','Inorm':'abs_normal_intensities'}

    def ret_dict(key):
        """
        Build up dictionary for each phoebe1 parameter, so that they
        correspond to the correct phoebe2 parameter.
        Args:
            key: The root of the key word used in phoebe1:
            component: the name of the phoebe2 component associated with
            the given mesh.

        Returns:
            dictionary of values which should be unique to a single
            parameter in phoebe 2.


        """
        d= {}
        comp = int(key[-1])
        key = key[:-1]
        #determine component
        if comp == 1:
            # TODO: is this hardcoding component names?  We should really access
            # from the hierarchy instead (we can safely assume a binary) by doing
            # b.hierarchy.get_stars() and b.hierarchy.get_primary_or_secondary()
            d['component'] = 'primary'
        elif comp== 2:
            d['component'] = 'secondary'
        else:
            #This really shouldn't happen
            raise ValueError("All mesh keys should be component specific.")
        try:
            d['qualifier'] = p2to1[key]
        except:
            d['qualifier'] = key
        if key == 'Inorm':
             d['unit'] = u.erg*u.s**-1*u.cm**-3
        return d


    def fill_mesh(mesh, type, time=None):
        """
        Fill phoebe2 mesh with values from phoebe1

        Args:
            key: Phoebe1 mesh for all time points
            type: mesh type "protomesh" or "automesh"
            time: array of times (only applicable for automesh)
        Returns:
            None

        Raises:
            ValueError if the anything other than automesh or protomesh is given for type.
        """
        keys = mesh.keys()

        if type == 'protomesh':
            grx1 = np.array(mesh['grx1'])
            gry1 = np.array(mesh['gry1'])
            grz1 = np.array(mesh['grz1'])
            # TODO: rewrite this to use np.linalg.norm
            grtot1 = grx1**2+gry1**2+grz1**2
            grx2 = np.array(mesh['grx2'])
            gry2 = np.array(mesh['gry2'])
            grz2 = np.array(mesh['grz2'])
            grtot2 = grx2**2+gry2**2+grz2**2
            grtot = [np.sqrt(grtot1),np.sqrt(grtot2)]

        for key in keys:
            d = ret_dict(key)
     #       key_values =  np.array_split(mesh[key],n)
            if type == 'protomesh':
                # take care of the protomesh
                prot_val = np.array(mesh[key])#key_values[-1]

                d['dataset'] = 'protomesh'
                if 'vcy' or 'gry' in key:
                    key_val = np.array(zip(prot_val, prot_val, -prot_val, -prot_val, prot_val, prot_val, -prot_val, -prot_val)).flatten()
                if 'vcz' or 'grz' in key:
                    key_val = np.array(zip(prot_val, prot_val, prot_val, prot_val, -prot_val, -prot_val, -prot_val, -prot_val)).flatten()
                else:
                    key_val = np.array(zip(prot_val, prot_val, prot_val, prot_val, prot_val, prot_val, prot_val, prot_val)).flatten()

                if key[:2] =='gr':
                    grtotn = grtot[int(key[-1])-1]

                    grtotn = np.array(zip(grtotn, grtotn, grtotn, grtotn, grtotn, grtotn, grtotn, grtotn)).flatten()

                    # normals should be normalized
                    d['value'] = -key_val /grtotn
                else:
                    d['value'] = key_val
                         #TODO fill the normals column it is just (nx, ny, nz)

                try:
                    new_syns.set_value(**d)
                except:
                    logger.warning('{} has no corresponding value in phoebe 2 protomesh'.format(key))

            elif type == 'pbmesh':
                n = len(time)
                key_values =  np.array_split(mesh[key],n)
                #TODO change time inserted to time = time[:-1]
                for t in range(len(time)):
                # d = ret_dict(key)
                    d['dataset'] = 'pbmesh'
                    if key in ['Inorm1', 'Inorm2']:
                        d['dataset'] = dataset

                        d['times'] = time[t]
                        #prepare data
                        if key[:2] in ['vc', 'gr']:
                            # I need to change coordinates but not yet done
                            pass

                            #TODO Change these values so that they are placed in orbit

                        else:
                            key_val= np.array(key_values[t])
                            key_val = np.array(zip(key_val, key_val, key_val, key_val, key_val, key_val, key_val, key_val)).flatten()

                            param = new_syns.filter(**d)
                            if param:
                                d['value'] = key_val
                                new_syns.set_value(**d)
                            else:
                                logger.warning('{} has no corresponding value in phoebe 2 automesh'.format(key))
            else:
                raise ValueError("Only 'pbmesh' and 'protomesh' are acceptable mesh types.")


        return

    # check whether phoebe legacy is installed
    if not _use_phb1:
        raise ImportError("phoebeBackend for phoebe legacy not found")

    computeparams = b.get_compute(compute, force_ps=True)
    protomesh = computeparams.get_value('protomesh', **kwargs)
    pbmesh = computeparams.get_value('pbmesh', **kwargs)
#    computeparams = b.get_compute(compute, force_ps=True)
#    hier = b.get_hierarchy()

#    starrefs  = hier.get_stars()
#    orbitrefs = hier.get_orbits()

    stars = b.hierarchy.get_stars()
    primary, secondary = stars
    #need for protomesh
    perpass = b.get_value(qualifier='t0_perpass', kind='orbit', context='component')
    # print primary, secondary
    #make phoebe 1 file

    # TODO BERT: this really should be a random name (tmpfile) so two instances won't clash
    io.pass_to_legacy(b, filename='_tmp_legacy_inp', compute=compute, **kwargs)
    phb1.init()
    try:
        phb1.configure()
    except SystemError:
        raise SystemError("PHOEBE config failed: try creating PHOEBE config file through GUI")
    phb1.open('_tmp_legacy_inp')
 #   phb1.updateLD()
    # TODO BERT: why are we saving here?
    # phb1.save('after.phoebe')
    lcnum = 0
    rvnum = 0
    infos, new_syns = _extract_from_bundle_by_dataset(b, compute=compute, times=times, protomesh=protomesh, pbmesh=pbmesh)


#    print "INFOS", len(infos)
#    print "info 1",  infos[0]
#    print "info 2-1",  infos[0][1]
#    print "info 3-1",  infos[0][2]
#    quit()
    if protomesh:
        time = [perpass]
        # print 'TIME', time
        phb1.setpar('phoebe_lcno', 1)
        flux, mesh = phb1.lc(tuple(time), 0, lcnum+1)
        fill_mesh(mesh, 'protomesh')

    for info in infos:
        info = info[0]
        this_syn = new_syns.filter(component=info['component'], dataset=info['dataset'])
        time = info['times']
        dataset=info['dataset']

        if info['kind'] == 'lc':
            if not pbmesh:
            # print "*********************", this_syn.qualifiers
                flux= np.array(phb1.lc(tuple(time.tolist()), lcnum))
                lcnum = lcnum+1
                #get rid of the extra periastron passage
                this_syn['fluxes'] = flux

            else:
            #    time = np.append(time, perpass)
            #    print "TIME", time, perpass
                flux, mesh = phb1.lc(tuple(time.tolist()), 0, lcnum+1)
                flux = np.array(flux)
            # take care of the lc first
                this_syn['fluxes'] = flux

                fill_mesh(mesh, 'pbmesh', time=time)
            # now deal with parameters
    #            keys = mesh.keys()
    #            n = len(time)


            # calculate the normal 'magnitude' for normalizing vectors
#                 grx1 = np.array_split(mesh['grx1'],n)[-1]
#                 gry1 = np.array_split(mesh['gry1'],n)[-1]
#                 grz1 = np.array_split(mesh['grz1'],n)[-1]
#                 # TODO: rewrite this to use np.linalg.norm
#                 grtot1 = grx1**2+gry1**2+grz1**2
#                 grx2 = np.array_split(mesh['grx1'],n)[-1]
#                 gry2 = np.array_split(mesh['gry1'],n)[-1]
#                 grz2 = np.array_split(mesh['grz1'],n)[-1]
#                 grtot2 = grx2**2+gry2**2+grz2**2
#                 grtot = [np.sqrt(grtot1),np.sqrt(grtot2)]
#                 for key in keys:
#                     key_values =  np.array_split(mesh[key],n)
#                     # take care of the protomesh
#                     prot_val = key_values[-1]
#                     d = ret_dict(key)
#                     d['dataset'] = 'protomesh'
#                     key_val = np.array(zip(prot_val, prot_val, prot_val, prot_val, prot_val, prot_val, prot_val, prot_val)).flatten()
#                     if key[:2] =='gr':
#                         grtotn = grtot[int(key[-1])-1]

#                         grtotn = np.array(zip(grtotn, grtotn, grtotn, grtotn, grtotn, grtotn, grtotn, grtotn)).flatten()

#                         # normals should be normalized
#                         d['value'] = -key_val /grtotn
#                     else:
#                         d['value'] = key_val
#                     #TODO fill the normals column it is just (nx, ny, nz)

#                     try:
#                         new_syns.set_value(**d)
#                     except:
#                         logger.warning('{} has no corresponding value in phoebe 2 protomesh'.format(key))

#                     #Normalize the normals that have been put in protomesh

#                     # now take care of automesh time point by time point
#                     for t in range(len(time[:-1])):
# #                        d = ret_dict(key)
#                         d['dataset'] = 'pbmesh'
#                         if key in ['Inorm1', 'Inorm2']:
#                             d['dataset'] = dataset

#                         d['times'] = time[t]
#                     #prepare data
#                         if key[:2] in ['vc', 'gr']:
#                             # I need to change coordinates but not yet done
#                             pass

#                             #TODO Change these values so that they are placed in orbit

#                         else:
#                             key_val= key_values[t]
#                             key_val = np.array(zip(key_val, key_val, key_val, key_val, key_val, key_val, key_val, key_val)).flatten()

#                             param = new_syns.filter(**d)
#                             if param:
#                                 d['value'] = key_val
#                                 new_syns.set_value(**d)
#                             else:
#                                 logger.warning('{} has no corresponding value in phoebe 2 automesh'.format(key))

#                 time = time[:-1]

        elif info['kind'] == 'rv':
#            print "SYN", this_syn
            rvid = info['dataset']
            #print "rvid", info
#            quit()

            if rvid == phb1.getpar('phoebe_rv_id', 0):

                dep =  phb1.getpar('phoebe_rv_dep', 0)
                dep = dep.split(' ')[0].lower()
           # must account for rv datasets with multiple components
                if dep != info['component']:
                    dep = info['component']

            elif rvid == phb1.getpar('phoebe_rv_id', 1):
                dep =  phb1.getpar('phoebe_rv_dep', 1)
                dep = dep.split(' ')[0].lower()
           # must account for rv datasets with multiple components
                if dep != info['component']:
                    dep = info['component']

            proximity = computeparams.filter(qualifier ='rv_method', component='primary', dataset=rvid).get_value()

            if proximity == 'flux-weighted':
                rveffects = 1
            else:
                rveffects = 0
#            try:
#                dep2 =  phb1.getpar('phoebe_rv_dep', 1)
#                dep2 = dep2.split(' ')[0].lower()
#            except:
#                dep2 = None
#            print "dep", dep
#            print "dep2", dep2
#            print "COMPONENT", info['component']
            if dep == 'primary':
                phb1.setpar('phoebe_proximity_rv1_switch', rveffects)
                rv = np.array(phb1.rv1(tuple(time.tolist()), 0))
                rvnum = rvnum+1

            elif dep == 'secondary':
                phb1.setpar('phoebe_proximity_rv2_switch', rveffects)
                rv = np.array(phb1.rv2(tuple(time.tolist()), 0))
                rvnum = rvnum+1
            else:
                raise ValueError(str(info['component'])+' is not the primary or the secondary star')


            #print "***", u.solRad.to(u.km)
            this_syn.set_value(qualifier='rvs', value=rv*u.km/u.s)
#            print "INFO", info
#            print "SYN", this_syn

            #print 'THIS SYN', this_syn

        elif info['kind']=='mesh':
            pass
#            print "I made it HERE"
#        if info['kind'] == 'mesh':
#            meshcol = {'tloc':'teff', 'glog':'logg','gr':'_o_normal_', 'vc':'_o_center' }

#            keys = ['tloc', 'glog']#, 'gr', 'vc']
#            n = len(time)
#            for i in keys:
#                p1keys, p2keys = par_build(i, info['component'])

#               for k in range(p1keys):
               # get parameter and copy because phoebe1 only does a quarter hemisphere.
#                    parn =  np.array_split(mesh[k],n)
#                    parn = np.array(zip(parn, parn, parn, parn, parn, parn, parn, parn)).flatten()

               # copy into correct location in phoebe2

                    #pary =  np.array_split(mesh[keyny],n)
                    #parz =  np.array_split(mesh[keynz],n)


#                for j in range(len(time)):

#                if i == 'gr' or i == 'vc':

#                    xd = i+'x'
#                    yd = i+'y'
#                    zd = i+'z'

#                par1x, par2x, parx = par_mesh(omesh, xd)
#                par1y, par2y, pary = par_mesh(omesh, yd)
#                par1z, par2z, parz = par_mesh(omesh, zd)

#                this_syn[meshcol[i]] = body.mesh[meshcol[i]]
#                this_syn['teff'] = body.mesh['teff']




    yield new_syns

