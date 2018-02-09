"""Routines and classes for estimation of observables."""

from __future__ import print_function

import numpy
import time
import copy
import warnings
# todo : handle more gracefully
try:
    from mpi4py import MPI
except ImportError:
    warnings.warn('No MPI library found')
import scipy.linalg
import os
import h5py
import pauxy.utils
import pauxy.propagation


class Estimators:
    """Container for qmc estimates of observables.

    Parameters
    ----------
    estimates : dict
        input options detailing which estimators to calculate.
    root : bool
        True if on root/master processor.
    uuid : string
        Calculation uuid.
    dt : float
        Timestep.
    nbasis : int
        Number of basis functions.
    nwalkers : int
        Number of walkers on this processor.
    json_string : string
        Information regarding input options.
    ghf : bool
        True is using GHF trial function.

    Attributes
    ----------
    header : list of strings
        Default estimates and simulation information.
    key : dict
        Explanation of output columns.
    nestimators : int
        Number of estimators.
    estimates : :class:`numpy.ndarray`
        Array containing accumulated estimates.
        See pauxy.estimators.Estimates.key for description.
    back_propagation : bool
        True if doing back propagation, specified in estimates dict.
    back_prop : :class:`pauxy.estimators.BackPropagation` object
        Class containing attributes and routines pertaining to back propagation.
    calc_itcf : bool
        True if calculating imaginary time correlation functions (ITCFs).
    itcf : :class:`pauxy.estimators.ITCF` object
        Class containing attributes and routines pertaining to back propagation.
    nprop_tot : int
        Total number of auxiliary field configurations we store / use for back
        propagation and itcf calculation.
    """

    def __init__(self, estimates, root, qmc, system, trial, BT2):
        if root:
            index = estimates.get('index', 0)
            h5f_name = estimates.get('filename', None)
            if h5f_name is None:
                overwrite = estimates.get('overwrite', True)
                h5f_name = 'estimates.%s.h5' % index
                while os.path.isfile(h5f_name) and not overwrite:
                    index = int(h5f_name.split('.')[1])
                    index = index + 1
                    h5f_name = 'estimates.%s.h5' % index
            self.h5f = h5py.File(h5f_name, 'w')
        else:
            self.h5f = None
        # Sub-members:
        # 1. Back-propagation
        mixed = estimates.get('mixed', {})
        bp = estimates.get('back_propagated', None)
        self.back_propagation = bp is not None
        self.estimators = {}
        dtype = complex
        self.estimators['mixed'] = Mixed(mixed, root, self.h5f,
                                         qmc, trial, dtype)
        if self.back_propagation:
            self.estimators['back_prop'] = BackPropagation(bp, root, self.h5f,
                                                           qmc, system, trial,
                                                           dtype, BT2)
            self.nprop_tot = self.estimators['back_prop'].nmax
            self.nbp = self.estimators['back_prop'].nmax
        else:
            self.nprop_tot = 1
            self.nbp = 1
        # 2. Imaginary time correlation functions.
        itcf = estimates.get('itcf', None)
        self.calc_itcf = itcf is not None
        if self.calc_itcf:
            self.estimators['itcf'] = ITCF(itcf, qmc, trial, root, self.h5f,
                                           system.nbasis, dtype,
                                           self.nprop_tot, BT2)
            self.nprop_tot = self.estimators['itcf'].nprop_tot

    def print_step(self, comm, nprocs, step, nmeasure):
        """Print QMC estimates.

        Parameters
        ----------
        state : :class:`pauxy.state.State`
            Simulation state.
        comm :
            MPI communicator.
        step : int
            Current iteration number.
        print_bp : bool (optional)
            If True we print out estimates relating to back propagation.
        print_itcf : bool (optional)
            If True we print out estimates relating to ITCFs.
        """
        for k, e in self.estimators.items():
            e.print_step(comm, nprocs, step, nmeasure)
        if (comm is None) or (comm.Get_rank() == 0):
            self.h5f.flush()

    def update(self, system, qmc, trial, psi, step, free_projection=False):
        for k, e in self.estimators.items():
            e.update(system, qmc, trial, psi, step, free_projection)


class EstimatorEnum:
    """Enum structure for help with indexing estimators array.

    python's support for enums doesn't help as it indexes from 1.
    """

    def __init__(self):
        # Exception for alignment of equal sign.
        self.weight = 0
        self.enumer = 1
        self.edenom = 2
        self.eproj = 3
        self.ekin = 4
        self.epot = 5
        self.time = 6


class Mixed:
    """Container for calculating mixed estimators.

    """

    def __init__(self, mixed, root, h5f, qmc, trial, dtype):
        self.rdm = mixed.get('rdm', False)
        self.nmeasure = qmc.nsteps // qmc.nmeasure
        self.header = ['iteration', 'Weight', 'E_num', 'E_denom', 'E',
                       'EKin', 'EPot', 'time']
        self.nreg = len(self.header[1:])
        self.G = numpy.zeros(trial.G.shape, trial.G.dtype)
        self.estimates = numpy.zeros(self.nreg + self.G.size, dtype=dtype)
        self.names = EstimatorEnum()
        self.estimates[self.names.time] = time.time()
        self.global_estimates = numpy.zeros(self.nreg + self.G.size,
                                            dtype=dtype)
        self.key = {
            'iteration': "Simulation iteration. iteration*dt = tau.",
            'Weight': "Total walker weight.",
            'E_num': "Numerator for projected energy estimator.",
            'E_denom': "Denominator for projected energy estimator.",
            'E': "Projected energy estimator.",
            'EKin': "Mixed kinetic energy estimator.",
            'EPot': "Mixed potential energy estimator.",
            'time': "Time per processor to complete one iteration.",
        }
        if root:
            energies = h5f.create_group('mixed_estimates')
            energies.create_dataset('headers',
                                    data=numpy.array(
                                        self.header[1:], dtype=object),
                                    dtype=h5py.special_dtype(vlen=str))
            self.output = H5EstimatorHelper(energies, 'energies',
                                            (self.nmeasure + 1, self.nreg),
                                            dtype)
            if self.rdm:
                self.dm_output = H5EstimatorHelper(energies, 'single_particle_greens_function',
                                                   (self.nmeasure + 1,) +
                                                   self.G.shape,
                                                   dtype)

    def update(self, system, qmc, trial, psi, step, free_projection=False):
        """Update regular estimates for walker w.

        Parameters
        ----------
        w : :class:`pauxy.walker.Walker`
            current walker
        state : :class:`pauxy.state.State`
            system parameters as well as current 'state' of the simulation.
        """
        if not free_projection:
            # When using importance sampling we only need to know the current
            # walkers weight as well as the local energy, the walker's overlap
            # with the trial wavefunction is not needed.
            for i, w in enumerate(psi.walkers):
                w.greens_function(trial)
                E, T, V = w.local_energy(system)
                self.estimates[self.names.enumer] += (
                        w.weight*E.real
                )
                self.estimates[self.names.ekin:self.names.epot+1] += (
                        w.weight*numpy.array([T,V]).real
                )
                self.estimates[self.names.weight] += w.weight
                self.estimates[self.names.edenom] += w.weight
                if self.rdm:
                    self.estimates[self.names.time+1:] += w.weight*w.G.flatten().real
        else:
            for i, w in enumerate(psi.walkers):
                w.greens_function(trial)
                self.estimates[self.names.enumer] += (
                        (w.weight*w.local_energy(system)[0]*w.ot)
                )
                self.estimates[self.names.weight] += w.weight
                self.estimates[self.names.edenom] += (w.weight*w.ot)

    def print_step(self, comm, nprocs, step, nmeasure):
        es = self.estimates
        ns = self.names
        denom = es[ns.edenom]*nprocs / nmeasure
        es[ns.eproj] = es[ns.enumer] / denom
        es[ns.ekin:ns.epot+1] /= denom
        es[ns.weight:ns.enumer] = es[ns.weight:ns.enumer]
        # Back propagated estimates
        es[ns.time] = (time.time()-es[ns.time]) / nprocs
        if comm is not None:
            comm.Reduce(es, self.global_estimates, op=MPI.SUM)
        else:
            self.global_estimates[:] = es
        # put these in own print routines.
        if (comm is None) or (comm.Get_rank() == 0):
            print (pauxy.utils.format_fixed_width_floats([step]+
                        list(self.global_estimates[:ns.time+1].real/nmeasure)))
            self.output.push(self.global_estimates[:ns.time+1]/nmeasure)
            if self.rdm:
                rdm = self.global_estimates[self.nreg:].reshape(self.G.shape)
                self.dm_output.push(rdm/denom/nmeasure)
        self.zero()

    def print_key(self, eol='', encode=False):
        """Print out information about what the estimates are.

        Parameters
        ----------
        key : dict
            Explanation of output columns.
        eol : string, optional
            String to append to output, e.g., Default : ''.
        encode : bool
            In True encode output to be utf-8.

        Returns
        -------
        None
        """
        header = (
            eol + '# Explanation of output column headers:\n' +
            '# -------------------------------------' + eol
        )
        if encode:
            header = header.encode('utf-8')
        print(header)
        for (k, v) in self.key.items():
            s = '# %s : %s' % (k, v) + eol
            if encode:
                s = s.encode('utf-8')
            print(s)

    def print_header(self, eol='', encode=False):
        r"""Print out header for estimators

        Parameters
        ----------
        header : list
            Output header.
        eol : string, optional
            String to append to output, Default : ''.
        encode : bool
            In True encode output to be utf-8.

        Returns
        -------
        None
        """
        s = pauxy.utils.format_fixed_width_strings(self.header) + eol
        if encode:
            s = s.encode('utf-8')
        print(s)

    def projected_energy(self):
        numerator = self.estimates[self.names.enumer]
        denominator = self.estimates[self.names.edenom]
        return (numerator / denominator).real

    def zero(self):
        self.estimates[:] = 0
        self.global_estimates[:] = 0
        self.estimates[self.names.time] = time.time()


class BackPropagation:
    """Container for performing back propagation.

    Parameters
    ----------
    bp : dict
        Input back propagation options :

        - nmax : int
            Number of back propagation steps to perform.

    root : bool
        True if on root/master processor.
    uuid : string
        Calculation uuid.
    json_string : string
        Information regarding input options.
    nsteps : int
        Total number of simulation steps.

    Attributes
    ----------
    header : list
        Header sfor back propagated estimators.
    estimates : :class:`numpy.ndarray`
        Container for local estimates.
    key : dict
        Explanation of output columns.
    funit : file
        Output file for back propagated estimates.
    """

    def __init__(self, bp, root, h5f, qmc, system, trial, dtype, BT2):
        self.nmax = bp.get('nback_prop', 0)
        self.header = ['iteration', 'weight', 'E', 'T', 'V']
        self.rdm = bp.get('rdm', False)
        self.nreg = len(self.header[1:])
        self.G = numpy.zeros(trial.G.shape, dtype=trial.G.dtype)
        self.estimates = numpy.zeros(
            self.nreg + self.G.size,
            dtype=trial.G.dtype)
        self.global_estimates = numpy.zeros(
            self.nreg + self.G.size, dtype=trial.G.dtype)
        self.nstblz = qmc.nstblz
        self.BT2 = BT2
        self.restore_weights = bp.get('restore_weights', None)
        self.dt = qmc.dt
        self.key = {
            'iteration': "Simulation iteration when back-propagation "
                         "measurement occured.",
            'E_var': "BP estimate for internal energy.",
            'T': "BP estimate for kinetic energy.",
            'V': "BP estimate for potential energy."
        }
        if root:
            energies = h5f.create_group('back_propagated_estimates')
            header = numpy.array(self.header[1:], dtype=object)
            energies.create_dataset('headers', data=header,
                                    dtype=h5py.special_dtype(vlen=str))
            self.output = H5EstimatorHelper(energies, 'energies',
                                            (qmc.nsteps//self.nmax, self.nreg),
                                            trial.G.dtype)
            if self.rdm:
                self.dm_output = H5EstimatorHelper(energies, 'single_particle_greens_function',
                                                  (qmc.nsteps//self.nmax,)+self.G.shape,
                                                  trial.G.dtype)
        if trial.type == 'GHF':
            self.update = self.update_ghf
            if system.name == "Generic":
                self.back_propagate = pauxy.propagation.back_propagate_generic_uhf
            else:
                self.back_propagate = pauxy.propagation.back_propagate_ghf
        else:
            self.update = self.update_uhf
            if system.name == "Generic":
                self.back_propagate = pauxy.propagation.back_propagate_generic
            else:
                self.back_propagate = pauxy.propagation.back_propagate

    def update_uhf(self, system, qmc, trial, psi, step, free_projection=False):
        r"""Calculate back-propagated "local" energy for given walker/determinant.

        Parameters
        ----------
        psi_nm : list of :class:`pauxy.walker.Walker` objects
            current distribution of walkers, i.e., at the current iteration in the
            simulation corresponding to :math:`\tau'=\tau+\tau_{bp}`.
        psi_n : list of :class:`pauxy.walker.Walker` objects
            previous distribution of walkers, i.e., at the current iteration in the
            simulation corresponding to :math:`\tau`.
        psi_bp : list of :class:`pauxy.walker.Walker` objects
            backpropagated walkers at time :math:`\tau_{bp}`.
        """
        if step % self.nmax != 0:
            return
        psi_bp = self.back_propagate(system, psi.walkers, trial,
                                     self.nstblz, self.BT2, qmc.dt)
        nup = system.nup
        denominator = 0
        for i, (wnm, wb) in enumerate(zip(psi.walkers, psi_bp)):
            self.G[0] = gab(wb.phi[:,:nup], wnm.phi_old[:,:nup]).T
            self.G[1] = gab(wb.phi[:,nup:], wnm.phi_old[:,nup:]).T
            energies = numpy.array(list(local_energy(system, self.G)))
            if self.restore_weights is not None:
                weight = wnm.weight * self.calculate_weight_factor(wnm)
            else:
                weight = wnm.weight
            denominator += weight
            self.estimates[1:] = (
                self.estimates[1:] + weight*numpy.append(energies,self.G.flatten())
            )
        self.estimates[0] += denominator
        psi.copy_historic_wfn()
        psi.copy_bp_wfn(psi_bp)

    def update_ghf(self, system, qmc, trial, psi, step, free_projection=False):
        r"""Calculate back-propagated "local" energy for given walker/determinant.

        Parameters
        ----------
        psi_nm : list of :class:`pauxy.walker.Walker` objects
            current distribution of walkers, i.e., at the current iteration in the
            simulation corresponding to :math:`\tau'=\tau+\tau_{bp}`.
        psi_n : list of :class:`pauxy.walker.Walker` objects
            previous distribution of walkers, i.e., at the current iteration in the
            simulation corresponding to :math:`\tau`.
        psi_bp : list of :class:`pauxy.walker.Walker` objects
            backpropagated walkers at time :math:`\tau_{bp}`.
        """
        if step % self.nmax != 0:
            return
        psi_bp = pauxy.propagation.back_propagate_ghf(system, psi.walkers, trial,
                                                      self.nstblz, self.BT2,
                                                      self.dt)
        denominator = sum(wnm.weight for wnm in psi.walkers)
        nup = system.nup
        for i, (wnm, wb) in enumerate(zip(psi.walkers, psi_bp)):
            construct_multi_ghf_gab(wb.phi, wnm.phi_old, wb.weights, wb.Gi, wb.ots)
            # note that we are abusing the weights variable from the multighf
            # walker to store the reorthogonalisation factors.
            weights = wb.weights * trial.coeffs * wb.ots
            denom = sum(weights)
            energies = numpy.array(list(local_energy_ghf(system, wb.Gi, weights, denom)))
            self.G = numpy.einsum('i,ijk->jk', weights, wb.Gi) / denom
            self.estimates[1:]= (
                self.estimates[1:] + wnm.weight*numpy.append(energies,self.G.flatten())
            )
        self.estimates[0] += denominator
        psi.copy_historic_wfn()
        psi.copy_bp_wfn(psi_bp)

    def calculate_weight_factor(self, walker):
        configs, cos_fac, weight_fac = walker.field_configs.get_block()
        factor = 1.0 + 0j
        for (w, c) in zip(weight_fac, cos_fac):
            factor *= w[0]
            if (self.restore_weights == "full"):
                factor /= c[0]
        return factor

    def print_step(self, comm, nprocs, step, nmeasure=1):
        if step != 0 and step % self.nmax == 0:
            comm.Reduce(self.estimates, self.global_estimates, op=MPI.SUM)
            if comm.Get_rank() == 0:
                self.output.push(self.global_estimates[:self.nreg]/(nprocs))
                if self.rdm:
                    rdm = self.global_estimates[self.nreg:].reshape(self.G.shape)/(nprocs)
                    self.dm_output.push(rdm)
            self.zero()

    def zero(self):
        self.estimates[:] = 0
        self.global_estimates[:] = 0


class ITCF:
    """ Container for calculating ITCFs.

    Parameters
    ----------
    itcf : dict
        Input itcf options:
            tmax : float
                Maximum value of imaginary time to calculate ITCF to.
            stable : bool
                If True use the stabalised algorithm of Feldbacher and Assad.
            mode : string / list
                How much of the ITCF to save to file:
                    'full' : print full ITCF.
                    'diagonal' : print diagonal elements of ITCF.
                    elements : list : print select elements defined from list.
            kspace : bool
                If True evaluate correlation functions in momentum space.
    dt : float
        Timestep.
    root : bool
        True if on root/master processor.
    uuid : string
        Calculation uuid.
    json_string : string
        Information regarding input options.
    nbasis : int
        Number of basis functions.

    Attributes
    ----------
    nmax : int
        Number of back propagation steps to perform.
    spgf : :class:`numpy.ndarray`
        Storage for single-particle greens function (SPGF).
    header : list
        Header sfor back propagated estimators.
    key : dict
        Explanation of output columns.
    rspace : hdf5 dataset
        Output dataset for real space itcfs.
    kspace : hdf5 dataset
        Output dataset for real space itcfs.
    """

    def __init__(self, itcf, qmc, trial, root, h5f, nbasis, dtype, nbp, BT2):
        self.stable = itcf.get('stable', True)
        self.tmax = itcf.get('tmax', 0.0)
        self.mode = itcf.get('mode', 'full')
        self.nmax = int(self.tmax/qmc.dt)
        self.nprop_tot = self.nmax + nbp
        self.nstblz = qmc.nstblz
        self.BT2 = BT2
        self.kspace = itcf.get('kspace', False)
        # self.spgf(i,j,k,l,m) gives the (l,m)th element of the spin-j(=0 for up
        # and 1 for down) k-ordered(0=greater,1=lesser) imaginary time green's
        # function at time i.
        # +1 in the first dimension is for the green's function at time tau = 0.
        self.spgf = numpy.zeros(shape=(self.nmax+1, 2, 2, nbasis, nbasis),
                                dtype=trial.G.dtype)
        self.spgf_global = numpy.zeros(shape=self.spgf.shape,
                                       dtype=trial.G.dtype)
        if trial.type == "GHF":
            self.I = numpy.identity(trial.psi.shape[1], dtype=trial.psi.dtype)
            self.initial_greens_function = self.initial_greens_function_ghf
            self.accumulate = self.accumulate_ghf
            self.back_propagate_single = pauxy.propagation.back_propagate_single_ghf
            self.construct_propagator_matrix = pauxy.propagation.construct_propagator_matrix_ghf
            if self.stable:
                self.increment_tau = self.increment_tau_ghf_stable
            else:
                self.increment_tau = self.increment_tau_ghf_unstable
        else:
            self.I = numpy.identity(trial.psi.shape[0], dtype=trial.psi.dtype)
            self.initial_greens_function = self.initial_greens_function_uhf
            self.accumulate = self.accumulate_uhf
            self.back_propagate_single = pauxy.propagation.back_propagate_single
            self.construct_propagator_matrix = pauxy.propagation.construct_propagator_matrix
            if self.stable:
                self.increment_tau = self.increment_tau_uhf_stable
            else:
                self.increment_tau = self.increment_tau_uhf_unstable
        if self.stable:
            self.calculate_spgf = self.calculate_spgf_stable
        else:
            self.calculate_spgf = self.calculate_spgf_unstable
        self.keys = [['up', 'down'], ['greater', 'lesser']]
        # I don't like list indexing so stick with numpy.
        if root:
            if self.mode == 'full':
                shape = (qmc.nsteps//(self.nmax),) + self.spgf.shape
            elif self.mode == 'diagonal':
                shape = (qmc.nsteps//(self.nmax), self.nmax+1, 2, 2, nbasis)
            else:
                shape = (qmc.nsteps//(self.nmax), self.nmax+1, 2, 2, len(self.mode))
            spgfs = h5f.create_group('single_particle_greens_function')
            self.rspace_unit = H5EstimatorHelper(spgfs, 'real_space', shape,
                                                 self.spgf.dtype)
            if self.kspace:
                self.kspace_unit = H5EstimatorHelper(spgfs, 'k_space', shape,
                                                     self.spgf.dtype)

    def update(self, system, qmc, trial, psi, step, free_projection=False):
        if step % self.nprop_tot == 0:
            self.calculate_spgf(system, psi, trial)

    def calculate_spgf_unstable(self, system, psi, trial):
        r"""Calculate imaginary time single-particle green's function.

        This uses the naive unstable algorithm.

        Parameters
        ----------
        state : :class:`pauxy.state.State`
            state object
        psi_left : list of :class:`pauxy.walker.Walker` objects
            backpropagated walkers projected to :math:`\tau_{bp}`.

        On return the spgf estimator array will have been updated.
        """

        nup = system.nup
        denom = sum(w.weight for w in psi.walkers)
        M = system.nbasis
        for ix, w in enumerate(psi.walkers):
            # 1. Construct psi_left for first step in algorithm by back
            # propagating the input back propagated left hand wfn.
            # Note we use the first nmax fields for estimating the ITCF.
            configs = w.field_configs.get_superblock()[0]
            self.back_propagate_single(w.phi_bp, configs, w.weights,
                                       system, self.nstblz, self.BT2)
            (Ggr, Gls) = self.initial_greens_function(w.phi_bp,
                                                      w.phi_init,
                                                      trial, nup,
                                                      w.weights)
            # 2. Calculate G(n,n). This is the equal time Green's function at
            # the step where we began saving auxilary fields (constructed with
            # psi_left back propagated along this path.)
            self.accumulate(0, w.weight, Ggr, Gls, M)
            # 3. Construct ITCF by moving forwards in imaginary time from time
            # slice n along our auxiliary field path.
            for (ic, c) in enumerate(configs):
                # B takes the state from time n to time n+1.
                B = self.construct_propagator_matrix(system, self.BT2, c)
                (Ggr, Gls) = self.increment_tau(Ggr, Gls, B)
                self.accumulate(ic+1, w.weight, Ggr, Gls, M)
        self.spgf = self.spgf / denom
        # copy current walker distribution to initial (right hand) wavefunction
        # for next estimate of ITCF
        psi.copy_init_wfn()

    def calculate_spgf_stable(self, system, psi, trial):
        """Calculate imaginary time single-particle green's function.

        This uses the stable algorithm as outlined in:
        Feldbacher and Assad, Phys. Rev. B 63, 073105.

        Parameters
        ----------
        state : :class:`pauxy.state.State`
            state object
        psi_left : list of :class:`pauxy.walker.Walker` objects
            backpropagated walkers projected to :math:`\tau_{bp}`.

        On return the spgf estimator array will have been updated.
        """

        nup = system.nup
        denom = sum(w.weight for w in psi.walkers)
        M = system.nbasis
        for ix, w in enumerate(psi.walkers):
            Ggr = numpy.identity(self.I.shape[0], dtype=self.I.dtype)
            Gls = numpy.identity(self.I.shape[0], dtype=self.I.dtype)
            # 1. Construct psi_L for first step in algorithm by back
            # propagating the input back propagated left hand wfn.
            # Note we use the first itcf_nmax fields for estimating the ITCF.
            # We store for intermediate back propagated left-hand wavefunctions.
            # This leads to more stable equal time green's functions compared to
            # that found by multiplying psi_L^n by B^{-1}(x^(n)) factors.
            configs = w.field_configs.get_superblock()[0]
            psi_Ls = self.back_propagate_single(w.phi_bp, configs, w.weights,
                                                system, self.nstblz, self.BT2,
                                                store=True)
            # 2. Calculate G(n,n). This is the equal time Green's function at
            # the step where we began saving auxilary fields (constructed with
            # psi_L back propagated along this path.)
            (Ggr_nn, Gls_nn) = self.initial_greens_function(w.phi_bp,
                                                            w.phi_init,
                                                            trial, nup,
                                                            w.weights)
            self.accumulate(0, w.weight, Ggr_nn, Gls_nn, M)
            # 3. Construct ITCF by moving forwards in imaginary time from time
            # slice n along our auxiliary field path.
            for (ic, c) in enumerate(configs):
                # B takes the state from time n to time n+1.
                B = self.construct_propagator_matrix(system, self.BT2, c)
                # G is the cumulative product of stabilised short-time ITCFs.
                # The first term in brackets is the G(n+1,n) which should be
                # well conditioned.
                (Ggr, Gls) = self.increment_tau(Ggr, Gls, B, Ggr_nn, Gls_nn)
                self.accumulate(ic+1, w.weight, Ggr, Gls, M)
                # Construct equal-time green's function shifted forwards along
                # the imaginary time interval. We need to update |psi_L> =
                # (B(c)^{dagger})^{-1}|psi_L> and |psi_R> = B(c)|psi_R>, where c
                # is the current configution in this loop. Note that we store
                # |psi_L> along the path, so we don't need to remove the
                # propagator matrices.
                L = psi_Ls[len(psi_Ls)-ic-1]
                pauxy.propagation.propagate_single(w.phi_init, system, B)
                if ic != 0 and ic % self.nstblz == 0:
                    (w.phi_init[:,:nup], R) = pauxy.utils.reortho(w.phi_init[:,:nup])
                    (w.phi_init[:,nup:], R) = pauxy.utils.reortho(w.phi_init[:,nup:])
                (Ggr_nn, Gls_nn) = self.initial_greens_function(L, w.phi_init,
                                                                trial, nup,
                                                                w.weights)
        self.spgf = self.spgf / denom
        # copy current walker distribution to initial (right hand) wavefunction
        # for next estimate of ITCF
        psi.copy_init_wfn()

    def initial_greens_function_uhf(self, A, B, trial, nup, weights):
        Ggr_up = self.I - gab(A[:,:nup], B[:,:nup])
        Ggr_down = self.I - gab(A[:,nup:], B[:,nup:])
        Gls_up = self.I - Ggr_up
        Gls_down = self.I - Ggr_down
        return (numpy.array([Ggr_up, Ggr_down]), numpy.array([Gls_up, Gls_down]))

    def initial_greens_function_ghf(self, A, B, trial, nup, weights):
        GAB = construct_multi_ghf_gab_back_prop(A, B, trial.coeffs, weights)
        Ggr = self.I - GAB
        Gls = self.I - Ggr
        return (Ggr, Gls)

    def accumulate_uhf(self, idx, weight, Ggr, Gls, nbasis):
        self.spgf[idx,0,0] += weight*Ggr[0].real
        self.spgf[idx,1,0] += weight*Ggr[1].real
        self.spgf[idx,0,1] += weight*Gls[0].real
        self.spgf[idx,1,1] += weight*Gls[1].real

    def accumulate_ghf(self, idx, weight, Ggr, Gls, nbasis):
        self.spgf[idx,0,0] += weight*Ggr[:nbasis,:nbasis].real
        self.spgf[idx,1,0] += weight*Ggr[nbasis:,nbasis:].real
        self.spgf[idx,0,1] += weight*Gls[:nbasis,:nbasis].real
        self.spgf[idx,1,1] += weight*Gls[nbasis:,nbasis:].real

    def increment_tau_ghf_unstable(self, Ggr, Gls, B, Gnn_gr=None, Gnn_ls=None):
        Ggr = B.dot(Ggr)
        Gls = Gls.dot(scipy.linalg.inv(B))
        return Ggr, Gls

    def increment_tau_uhf_unstable(self, Ggr, Gls, B, Gnn_gr=None, Gnn_ls=None):
        Ggr[0] = B[0].dot(Ggr[0])
        Ggr[1] = B[1].dot(Ggr[1])
        Gls[0] = Gls[0].dot(scipy.linalg.inv(B[0]))
        Gls[1] = Gls[1].dot(scipy.linalg.inv(B[1]))
        return Ggr, Gls

    def increment_tau_uhf_stable(self, Ggr, Gls, B, Gnn_gr, Gnn_ls):
        Ggr[0] = (B[0].dot(Gnn_gr[0])).dot(Ggr[0])
        Ggr[1] = (B[1].dot(Gnn_gr[1])).dot(Ggr[1])
        Gls[0] = Gls[0].dot(Gnn_ls[0].dot(scipy.linalg.inv(B[0])))
        Gls[1] = Gls[0].dot(Gnn_ls[1].dot(scipy.linalg.inv(B[1])))
        return Ggr, Gls

    def increment_tau_ghf_stable(self, Ggr, Gls, B, Gnn_gr, Gnn_ls):
        Ggr = (B.dot(Gnn_gr)).dot(Ggr)
        Gls = (Gnn_ls.dot(scipy.linalg.inv(B))).dot(Gls)
        return Ggr, Gls

    def print_step(self, comm, nprocs, step, nmeasure=1):
        if step != 0 and step % self.nprop_tot == 0:
            comm.Reduce(self.spgf, self.spgf_global, op=MPI.SUM)
            if comm.Get_rank() == 0:
                self.to_file(self.rspace_unit, self.spgf_global/nprocs)
                if self.kspace:
                    M = self.spgf.shape[-1]
                    # FFT the real space Green's function.
                    # Todo : could just use numpy.fft.fft....
                    # spgf_k = numpy.einsum('ik,rqpkl,lj->rqpij', self.P,
                    # spgf, self.P.conj().T) / M
                    spgf_k = numpy.fft.fft2(self.spgf_global)
                    if self.spgf.dtype == complex:
                        self.to_file(self.kspace_unit, spgf_k/nprocs)
                    else:
                        self.to_file(self.kspace_unit, spgf_k.real/nprocs)
            self.zero()

    def to_file(self, group, spgf):
        """Push ITCF to hdf5 group.

        Parameters
        ----------
        group: string
            HDF5 group name.
        spgf : :class:`numpy.ndarray`
            Single-particle Green's function (SPGF).
        """
        if self.mode == 'full':
            group.push(spgf)
        elif self.mode == 'diagonal':
            group.push(spgf.diagonal(axis1=3, axis2=4))
        else:
            group.push(numpy.array([g[mode] for g in spgf]))

    def zero(self):
        self.spgf[:] = 0
        self.spgf_global[:] = 0

def local_energy(system, G):
    ghf = (G.shape[-1] == 2*system.nbasis)
    if system.name == "Hubbard":
        if ghf:
            return local_energy_ghf(system, G)
        else:
            return local_energy_hubbard(system, G)
    else:
        return local_energy_generic(system, G)


def local_energy_hubbard(system, G):
    r"""Calculate local energy of walker for the Hubbard model.

    Parameters
    ----------
    system : :class:`Hubbard`
        System information for the Hubbard model.
    G : :class:`numpy.ndarray`
        Walker's "Green's function"

    Returns
    -------
    (E_L(phi), T, V): tuple
        Local, kinetic and potential energies of given walker phi.
    """
    ke = numpy.sum(system.T[0] * G[0] + system.T[1] * G[1])
    pe = sum(system.U * G[0][i][i] * G[1][i][i]
             for i in range(0, system.nbasis))

    return (ke + pe, ke, pe)


def local_energy_ghf(system, Gi, weights, denom):
    """Calculate local energy of GHF walker for the Hubbard model.

    Parameters
    ----------
    system : :class:`Hubbard`
        System information for the Hubbard model.
    Gi : :class:`numpy.ndarray`
        Array of Walker's "Green's function"
    denom : float
        Overlap of trial wavefunction with walker.

    Returns
    -------
    (E_L(phi), T, V): tuple
        Local, kinetic and potential energies of given walker phi.
    """
    ke = numpy.einsum('i,ikl,kl->', weights, Gi, system.Text) / denom
    # numpy.diagonal returns a view so there should be no overhead in creating
    # temporary arrays.
    guu = numpy.diagonal(Gi[:,:system.nbasis,:system.nbasis], axis1=1, axis2=2)
    gdd = numpy.diagonal(Gi[:,system.nbasis:,system.nbasis:], axis1=1, axis2=2)
    gud = numpy.diagonal(Gi[:,system.nbasis:,:system.nbasis], axis1=1, axis2=2)
    gdu = numpy.diagonal(Gi[:,:system.nbasis,system.nbasis:], axis1=1, axis2=2)
    gdiag = guu*gdd - gud*gdu
    pe = system.U * numpy.einsum('j,jk->', weights, gdiag) / denom
    return (ke+pe, ke, pe)


def local_energy_multi_det(system, Gi, weights):
    """Calculate local energy of GHF walker for the Hubbard model.

    Parameters
    ----------
    system : :class:`Hubbard`
        System information for the Hubbard model.
    Gi : :class:`numpy.ndarray`
        Array of Walker's "Green's function"
    weights : :class:`numpy.ndarray`
        Components of overlap of trial wavefunction with walker.

    Returns
    -------
    (E_L(phi), T, V): tuple
        Local, kinetic and potential energies of given walker phi.
    """
    denom = numpy.sum(weights)
    ke = numpy.einsum('i,ikl,kl->', weights, Gi, system.Text) / denom
    # numpy.diagonal returns a view so there should be no overhead in creating
    # temporary arrays.
    guu = numpy.diagonal(Gi[:,:,:system.nup], axis1=1,
                         axis2=2)
    gdd = numpy.diagonal(Gi[:,:,system.nup:], axis1=1,
                         axis2=2)
    pe = system.U * numpy.einsum('j,jk->', weights, guu*gdd) / denom
    return (ke+pe, ke, pe)

def local_energy_ghf_full(system, GAB, weights):
    r"""Calculate local energy of GHF walker for the Hubbard model.

    Parameters
    ----------
    system : :class:`Hubbard`
        System information for the Hubbard model.
    GAB : :class:`numpy.ndarray`
        Matrix of Green's functions for different SDs A and B.
    weights : :class:`numpy.ndarray`
        Components of overlap of trial wavefunction with walker.

    Returns
    -------
    (E_L, T, V): tuple
        Local, kinetic and potential energies of given walker phi.
    """
    denom = numpy.sum(weights)
    ke = numpy.einsum('ij,ijkl,kl->', weights, GAB, system.Text) / denom
    # numpy.diagonal returns a view so there should be no overhead in creating
    # temporary arrays.
    guu = numpy.diagonal(GAB[:,:,:system.nbasis,:system.nbasis], axis1=2,
                         axis2=3)
    gdd = numpy.diagonal(GAB[:,:,system.nbasis:,system.nbasis:], axis1=2,
                         axis2=3)
    gud = numpy.diagonal(GAB[:,:,system.nbasis:,:system.nbasis], axis1=2,
                         axis2=3)
    gdu = numpy.diagonal(GAB[:,:,:system.nbasis,system.nbasis:], axis1=2,
                         axis2=3)
    gdiag = guu*gdd - gud*gdu
    pe = system.U * numpy.einsum('ij,ijk->', weights, gdiag) / denom
    return (ke+pe, ke, pe)

def gab(A, B):
    r"""One-particle Green's function.

    This actually returns 1-G since it's more useful, i.e.,

    .. math::
        \langle \phi_A|c_i^{\dagger}c_j|\phi_B\rangle =
        [B(A^{\dagger}B)^{-1}A^{\dagger}]_{ji}

    where :math:`A,B` are the matrices representing the Slater determinants
    :math:`|\psi_{A,B}\rangle`.

    For example, usually A would represent (an element of) the trial wavefunction.

    .. warning::
        Assumes A and B are not orthogonal.

    Parameters
    ----------
    A : :class:`numpy.ndarray`
        Matrix representation of the bra used to construct G.
    B : :class:`numpy.ndarray`
        Matrix representation of the ket used to construct G.

    Returns
    -------
    GAB : :class:`numpy.ndarray`
        (One minus) the green's function.
    """
    # Todo: check energy evaluation at later point, i.e., if this needs to be
    # transposed. Shouldn't matter for Hubbard model.
    inv_O = scipy.linalg.inv((A.conj().T).dot(B))
    GAB = B.dot(inv_O.dot(A.conj().T))
    return GAB


def gab_mod(A, B):
    r"""One-particle Green's function.

    This actually returns 1-G since it's more useful, i.e.,

    .. math::
        \langle \phi_A|c_i^{\dagger}c_j|\phi_B\rangle =
        [B(A^{\dagger}B)^{-1}A^{\dagger}]_{ji}

    where :math:`A,B` are the matrices representing the Slater determinants
    :math:`|\psi_{A,B}\rangle`.

    For example, usually A would represent (an element of) the trial wavefunction.

    .. warning::
        Assumes A and B are not orthogonal.

    Parameters
    ----------
    A : :class:`numpy.ndarray`
        Matrix representation of the bra used to construct G.
    B : :class:`numpy.ndarray`
        Matrix representation of the ket used to construct G.

    Returns
    -------
    GAB : :class:`numpy.ndarray`
        (One minus) the green's function.
    """
    # Todo: check energy evaluation at later point, i.e., if this needs to be
    # transposed. Shouldn't matter for Hubbard model.
    inv_O = scipy.linalg.inv((A.conj().T).dot(B))
    GAB = B.dot(inv_O)
    return GAB


def gab_multi_det(A, B, coeffs):
    r"""One-particle Green's function.

    This actually returns 1-G since it's more useful, i.e.,

    .. math::
        \langle \phi_A|c_i^{\dagger}c_j|\phi_B\rangle = [B(A^{*T}B)^{-1}A^{*T}]_{ji}

    where :math:`A,B` are the matrices representing the Slater determinants
    :math:`|\psi_{A,B}\rangle`.

    For example, usually A would represent a multi-determinant trial wavefunction.

    .. warning::
        Assumes A and B are not orthogonal.

    Parameters
    ----------
    A : :class:`numpy.ndarray`
        Numpy array of the Matrix representation of the elements of the bra used
        to construct G.
    B : :class:`numpy.ndarray`
        Matrix representation of the ket used to construct G.

    Returns
    -------
    GAB : :class:`numpy.ndarray`
        (One minus) the green's function.
    """
    # Todo: check energy evaluation at later point, i.e., if this needs to be
    # transposed. Shouldn't matter for Hubbard model.
    Gi = numpy.zeros(A.shape)
    overlaps = numpy.zeros(A.shape[1])
    for (ix, Aix) in enumerate(A):
        # construct "local" green's functions for each component of A
        # Todo: list comprehension here.
        inv_O = scipy.linalg.inv((Aix.conj().T).dot(B))
        Gi[ix] = (B.dot(inv_O.dot(Aix.conj().T))).T
        overlaps[ix] = 1.0 / scipy.linalg.det(inv_O)
    denom = numpy.dot(coeffs, overlaps)
    return numpy.einsum('i,ijk,i->jk', coeffs, Gi, overlaps) / denom


def construct_multi_ghf_gab_back_prop(A, B, coeffs, bp_weights):
    M = A.shape[1] // 2
    Gi, overlaps = construct_multi_ghf_gab(A, B, coeffs)
    scale = max(max(bp_weights), max(overlaps))
    full_weights = bp_weights * coeffs * overlaps / scale
    denom = sum(full_weights)
    G = numpy.einsum('i,ijk->jk', full_weights, Gi) / denom

    return G


def construct_multi_ghf_gab(A, B, coeffs, Gi=None, overlaps=None):
    M = B.shape[0] // 2
    if Gi is None:
        Gi = numpy.zeros(shape=(A.shape[0],A.shape[1],A.shape[1]), dtype=A.dtype)
    if overlaps is None:
        overlaps = numpy.zeros(A.shape[0], dtype=A.dtype)
    for (ix, Aix) in enumerate(A):
        # construct "local" green's functions for each component of A
        # Todo: list comprehension here.
        inv_O = scipy.linalg.inv((Aix.conj().T).dot(B))
        Gi[ix] = (B.dot(inv_O.dot(Aix.conj().T)))
        overlaps[ix] = 1.0 / scipy.linalg.det(inv_O)
    return (Gi, overlaps)


def gab_multi_det_full(A, B, coeffsA, coeffsB, GAB, weights):
    r"""One-particle Green's function.

    This actually returns 1-G since it's more useful, i.e.,

    .. math::
        \langle \phi_A|c_i^{\dagger}c_j|\phi_B\rangle = [B(A^{*T}B)^{-1}A^{*T}]_{ji}

    where :math:`A,B` are the matrices representing the Slater determinants
    :math:`|\psi_{A,B}\rangle`.

    .. todo: Fix docstring

    Here we assume both A and B are multi-determinant expansions.

    .. warning::
        Assumes A and B are not orthogonal.

    Parameters
    ----------
    A : :class:`numpy.ndarray`
        Numpy array of the Matrix representation of the elements of the bra used
        to construct G.
    B : :class:`numpy.ndarray`
        Array containing elements of multi-determinant matrix representation of
        the ket used to construct G.

    Returns
    -------
    GAB : :class:`numpy.ndarray`
        (One minus) the green's function.
    """
    for ix, (Aix, cix) in enumerate(zip(A, coeffsA)):
        for iy, (Biy, ciy) in enumerate(zip(B, coeffsB)):
            # construct "local" green's functions for each component of A
            inv_O = scipy.linalg.inv((Aix.conj().T).dot(Biy))
            GAB[ix,iy] = (Biy.dot(inv_O)).dot(Aix.conj().T)
            weights[ix,iy] =  cix*(ciy.conj()) / scipy.linalg.det(inv_O)
    denom = numpy.sum(weights)
    G = numpy.einsum('ij,ijkl->kl', weights, GAB) / denom
    return G


def eproj(estimates, enum):
    """Real projected energy.

    Parameters
    ----------
    estimates : numpy.array
        Array containing estimates averaged over all processors.
    enum : :class:`pauxy.estimators.EstimatorEnum` object
        Enumerator class outlining indices of estimates array elements.

    Returns
    -------
    eproj : float
        Projected energy from current estimates array.
    """

    numerator = estimates[enum.enumer]
    denominator = estimates[enum.edenom]
    return (numerator/denominator).real

class H5EstimatorHelper:
    def __init__(self, h5f, name, shape, dtype):
        self.store = h5f.create_dataset(name, shape, dtype=dtype)
        self.index = 0

    def push(self, data):
        self.store[self.index] = data
        self.index = self.index + 1

def local_energy_generic(system, G):
    """Local energy for generic two-body Hamiltonian"""
    e1 = (numpy.einsum('ij,ji->', system.T[0], G[0]) +
          numpy.einsum('ij,ji->', system.T[1], G[1]))
    euu = 0.5*(numpy.einsum('pqrs,pr,qs->', system.h2e, G[0], G[0]) -
               numpy.einsum('pqrs,ps,qr->', system.h2e, G[0], G[0]))
    edd = 0.5*(numpy.einsum('pqrs,pr,qs->', system.h2e, G[1], G[1]) -
               numpy.einsum('pqrs,ps,qr->', system.h2e, G[1], G[1]))
    eud = 0.5*numpy.einsum('pqrs,pr,qs->', system.h2e, G[0], G[1])
    edu = 0.5*numpy.einsum('pqrs,pr,qs->', system.h2e, G[1], G[0])
    e2 = euu + edd + eud + edu
    return (e1+e2+system.ecore, e1+system.ecore, e2)

def local_energy_generic_cholesky(system, G):
    """Local energy for generic two-body (cholesky decomposed) Hamiltonian"""
    e1 = (numpy.einsum('ij,ji->', system.T[0], G[0]) +
          numpy.einsum('ij,ji->', system.T[1], G[1]))
    euu = 0.5*(numpy.einsum('lpr,lqs,pr,qs->', system.chol_vecs,
                            system.chol_vecs, G[0], G[0]) -
               numpy.einsum('lpr,lqs,ps,qr->', system.chol_vecs,
                            system.chol_vecs, G[0], G[0]))
    edd = 0.5*(numpy.einsum('lpr,lqs,pr,qs->', system.chol_vecs,
                            system.chol_vecs, G[1], G[1]) -
               numpy.einsum('lpr,lqs,ps,qr->', system.chol_vecs,
                            system.chol_vecs, G[1], G[1]))
    eud = 0.5*numpy.einsum('lpr,lqs,pr,qs->', system.chol_vecs,
                           system.chol_vecs, G[0], G[1])
    edu = 0.5*numpy.einsum('lpr,lqs,pr,qs->', system.chol_vecs,
                           system.chol_vecs, G[1], G[0])
    e2 = euu + edd + eud + edu
    return (e1+e2+system.ecore, e1+system.ecore, e2)

def local_energy_generic_cholesky_opt(system, Theta, L):
    """Local energy for generic two-body (cholesky decomposed) Hamiltonian"""
    e1 = (numpy.einsum('ij,ji->', system.T[0], G[0]) +
          numpy.einsum('ij,ji->', system.T[1], G[1]))
    euu = 0.5*(numpy.einsum('lpr,lqs,pr,qs->', system.chol_vecs,
                            system.chol_vecs, G[0], G[0]) -
               numpy.einsum('lpr,lqs,ps,qr->', system.chol_vecs,
                            system.chol_vecs, G[0], G[0]))
    edd = 0.5*(numpy.einsum('lpr,lqs,pr,qs->', system.chol_vecs,
                            system.chol_vecs, G[1], G[1]) -
               numpy.einsum('lpr,lqs,ps,qr->', system.chol_vecs,
                            system.chol_vecs, G[1], G[1]))
    eud = 0.5*numpy.einsum('lpr,lqs,pr,qs->', system.chol_vecs,
                           system.chol_vecs, G[0], G[1])
    edu = 0.5*numpy.einsum('lpr,lqs,pr,qs->', system.chol_vecs,
                           system.chol_vecs, G[1], G[0])
    e2 = euu + edd + eud + edu
    return (e1+e2+system.ecore, e1+system.ecore, e2)