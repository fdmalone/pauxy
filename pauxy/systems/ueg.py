import sys
import numpy
import scipy.linalg
import scipy.sparse
import pauxy.utils
import math
import time
from pauxy.utils.io import dump_qmcpack_cholesky


class UEG(object):
    """UEG system class (integrals read from fcidump)
    Parameters
    ----------
    nup : int
        Number of up electrons.
    ndown : int
        Number of down electrons.
    rs : float
        Density parameter.
    ecut : float
        Scaled cutoff energy.
    ktwist : :class:`numpy.ndarray`
        Twist vector.
    verbose : bool
        Print extra information.
    Attributes
    ----------
    T : :class:`numpy.ndarray`
        One-body part of the Hamiltonian. This is diagonal in plane wave basis.
    ecore : float
        Madelung contribution to the total energy.
    h1e_mod : :class:`numpy.ndarray`
        Modified one-body Hamiltonian.
    nfields : int
        Number of field configurations per walker for back propagation.
    basis : :class:`numpy.ndarray`
        Basis vectors within a cutoff.
    kfac : float
        Scale factor (2pi/L).
    """

    def __init__(self, inputs, verbose=False):
        if verbose:
            print("# Parsing input options.")
        self.name = "UEG"
        self.nup = inputs.get('nup')
        self.ndown = inputs.get('ndown')
        self.nelec = (self.nup,self.ndown)
        self.rs = inputs.get('rs')
        self.ecut = inputs.get('ecut')
        self.ktwist = numpy.array(inputs.get('ktwist', [0,0,0])).reshape(3)
        self.mu = inputs.get('mu', None)
        if verbose:
            print("# Number of spin-up electrons: %i"%self.nup)
            print("# Number of spin-down electrons: %i"%self.ndown)
            print("# rs: %10.5f"%self.rs)

        self.thermal = inputs.get('thermal', False)
        if self.thermal and verbose:
            print("# Thermal UEG activated")
        self._alt_convention = inputs.get('alt_convention', False)
        self.sparse = True

        # total # of electrons
        self.ne = self.nup + self.ndown
        # core energy
        self.ecore = 0.5 * self.ne * self.madelung()
        # spin polarisation
        self.zeta = (self.nup - self.ndown) / self.ne
        # Density.
        self.rho = ((4.0*math.pi)/3.0*self.rs**3.0)**(-1.0)
        # Box Length.
        self.L = self.rs*(4.0*self.ne*math.pi/3.)**(1/3.)
        # Volume
        self.vol = self.L**3.0
        # k-space grid spacing.
        # self.kfac = 2*math.pi/self.L
        self.kfac = 2*math.pi/self.L
        # Fermi Wavevector (infinite system).
        self.kf = (3*(self.zeta+1)*math.pi**2*self.ne/self.L**3)**(1/3.)
        # Fermi energy (inifinite systems).
        self.ef = 0.5*self.kf**2
        self.diagH1 = True

        skip_cholesky = inputs.get('skip_cholesky', False)
        if verbose:
            print("# Spin polarisation (zeta): %d"%self.zeta)
            print("# Electron density (rho): %13.8e"%self.rho)
            print("# Box Length (L): %13.8e"%self.L)
            print("# Volume: %13.8e"%self.vol)
            print("# k-space factor (2pi/L): %13.8e"%self.kfac)
            print("# Madelung Energy: %13.8e"%self.ecore)

        # Single particle eigenvalues and corresponding kvectors
        (self.sp_eigv, self.basis, self.nmax) = self.sp_energies(self.kfac, self.ecut)

        self.shifted_nmax = 2*self.nmax
        self.imax_sq = numpy.dot(self.basis[-1], self.basis[-1])
        self.create_lookup_table()
        for (i, k) in enumerate(self.basis):
            assert(i==self.lookup_basis(k))

        # Number of plane waves.
        self.nbasis = len(self.sp_eigv)
        self.nactive = self.nbasis
        self.ncore = 0
        self.nfv = 0
        self.mo_coeff = None
        # Allowed momentum transfers (4*ecut)
        (eigs, qvecs, self.qnmax) = self.sp_energies(self.kfac, 4*self.ecut)
        # Omit Q = 0 term.
        self.qvecs = numpy.copy(qvecs[1:])
        self.vqvec = numpy.array([self.vq(self.kfac*q) for q in self.qvecs])
        # Number of momentum transfer vectors / auxiliary fields.
        # Can reduce by symmetry but be stupid for the moment.
        self.nchol = len(self.qvecs)
        self.nfields = 2*len(self.qvecs)
        if verbose:
            print("# Number of plane waves: %i"%self.nbasis)
            print("# Number of Cholesky vectors: %i"%self.nchol)
        # For consistency with frozen core molecular code.
        self.orbs = None
        self.frozen_core = False
        T = numpy.diag(self.sp_eigv)
        self.H1 = numpy.array([T, T]) # Making alpha and beta

        if (skip_cholesky == False):
            h1e_mod = self.mod_one_body(T)
            self.h1e_mod = numpy.array([h1e_mod, h1e_mod])
        self.orbs = None
        self._opt = True


        nlimit = self.nup

        if self.thermal:
            nlimit = self.nbasis

        self.ikpq_i = []
        self.ikpq_kpq = []
        for (iq, q) in enumerate(self.qvecs):
            idxkpq_list_i =[]
            idxkpq_list_kpq =[]
            for i, k in enumerate(self.basis[0:nlimit]):
                kpq = k + q
                idxkpq = self.lookup_basis(kpq)
                if idxkpq is not None:
                    idxkpq_list_i += [i]
                    idxkpq_list_kpq += [idxkpq]
            self.ikpq_i += [idxkpq_list_i]
            self.ikpq_kpq += [idxkpq_list_kpq]

        self.ipmq_i = []
        self.ipmq_pmq = []
        for (iq, q) in enumerate(self.qvecs):
            idxpmq_list_i =[]
            idxpmq_list_pmq =[]
            for i, p in enumerate(self.basis[0:nlimit]):
                pmq = p - q
                idxpmq = self.lookup_basis(pmq)
                if idxpmq is not None:
                    idxpmq_list_i += [i]
                    idxpmq_list_pmq += [idxpmq]
            self.ipmq_i += [idxpmq_list_i]
            self.ipmq_pmq += [idxpmq_list_pmq]

        for (iq, q) in enumerate(self.qvecs):
            self.ikpq_i[iq]  = numpy.array(self.ikpq_i[iq], dtype=numpy.int64)
            self.ikpq_kpq[iq] = numpy.array(self.ikpq_kpq[iq], dtype=numpy.int64)
            self.ipmq_i[iq]  = numpy.array(self.ipmq_i[iq], dtype=numpy.int64)
            self.ipmq_pmq[iq] = numpy.array(self.ipmq_pmq[iq], dtype=numpy.int64)


        if (skip_cholesky == False):
            if verbose:
                print("# Constructing two-body potentials incore.")
            (self.chol_vecs, self.iA, self.iB) = self.two_body_potentials_incore()
            write_ints = inputs.get('write_integrals', None)
            if write_ints is not None:
                self.write_integrals()
            if verbose:
                print("# Approximate memory required for "
                      "two-body potentials: %f GB."%(3*self.iA.nnz*16/(1024**3)))
                print("# Constructing two_body_potentials_incore finished")
                print("# Finished setting up UEG system object.")


    def sp_energies(self, kfac, ecut):
        """Calculate the allowed kvectors and resulting single particle eigenvalues (basically kinetic energy)
        which can fit in the sphere in kspace determined by ecut.
        Parameters
        ----------
        kfac : float
            kspace grid spacing.
        ecut : float
            energy cutoff.
        Returns
        -------
        spval : :class:`numpy.ndarray`
            Array containing sorted single particle eigenvalues.
        kval : :class:`numpy.ndarray`
            Array containing basis vectors, sorted according to their
            corresponding single-particle energy.
        """

        # Scaled Units to match with HANDE.
        # So ecut is measured in units of 1/kfac^2.
        nmax = int(math.ceil(numpy.sqrt((2*ecut))))

        spval = []
        vec = []
        kval = []
        ks = self.ktwist

        for ni in range(-nmax, nmax+1):
            for nj in range(-nmax, nmax+1):
                for nk in range(-nmax, nmax+1):
                    spe = 0.5*(ni**2 + nj**2 + nk**2)
                    if (spe <= ecut):
                        kijk = [ni,nj,nk]
                        kval.append(kijk)
                        # Reintroduce 2 \pi / L factor.
                        ek = 0.5*numpy.dot(numpy.array(kijk)+ks,
                                           numpy.array(kijk)+ks)
                        spval.append(kfac**2*ek)

        # Sort the arrays in terms of increasing energy.
        spval = numpy.array(spval)
        ix = numpy.argsort(spval, kind='mergesort')
        spval = spval[ix]
        kval = numpy.array(kval)[ix]

        return (spval, kval, nmax)

    def create_lookup_table(self):
        basis_ix = []
        for k in self.basis:
            basis_ix.append(self.map_basis_to_index(k))
        self.lookup = numpy.zeros(max(basis_ix)+1, dtype=int)
        for i, b in enumerate(basis_ix):
            self.lookup[b] = i
        self.max_ix = max(basis_ix)

    def lookup_basis(self, vec):
        if (numpy.dot(vec,vec) <= self.imax_sq):
            ix = self.map_basis_to_index(vec)
            if ix >= len(self.lookup):
                ib = None
            else:
                ib = self.lookup[ix]
            return ib
        else:
            ib = None

    def map_basis_to_index(self, k):
        return ((k[0]+self.nmax) +
                self.shifted_nmax*(k[1]+self.nmax) +
                self.shifted_nmax*self.shifted_nmax*(k[2]+self.nmax))

    def madelung(self):
        """Use expression in Schoof et al. (PhysRevLett.115.130402) for the
        Madelung contribution to the total energy fitted to L.M. Fraser et al.
        Phys. Rev. B 53, 1814.
        Parameters
        ----------
        rs : float
            Wigner-Seitz radius.
        ne : int
            Number of electrons.
        Returns
        -------
        v_M: float
            Madelung potential (in Hartrees).
        """
        c1 = -2.837297
        c2 = (3.0/(4.0*math.pi))**(1.0/3.0)
        return c1 * c2 / (self.ne**(1.0/3.0) * self.rs)

    def vq(self, q):
        """The typical 3D Coulomb kernel
        Parameters
        ----------
        q : float
            a plane-wave vector
        Returns
        -------
        v_M: float
            3D Coulomb kernel (in Hartrees)
        """
        return 4*math.pi / numpy.dot(q, q)

    def mod_one_body(self, T):
        """ Add a diagonal term of two-body Hamiltonian to the one-body term
        Parameters
        ----------
        T : float
            one-body Hamiltonian (i.e. kinetic energy)
        Returns
        -------
        h1e_mod: float
            modified one-body Hamiltonian
        """
        h1e_mod = numpy.copy(T)

        fac = 1.0 / (2.0 * self.vol)
        for (i, ki) in enumerate(self.basis):
            for (j, kj) in enumerate(self.basis):
                if i != j:
                    q = self.kfac * (ki - kj)
                    h1e_mod[i,i] = h1e_mod[i,i] - fac * self.vq(q)
        return h1e_mod

    def density_operator(self, iq):
        """ Density operator as defined in Eq.(6) of PRB(75)245123
        Parameters
        ----------
        q : float
            a plane-wave vector
        Returns
        -------
        rho_q: float
            density operator
        """
        nnz = self.rho_ikpq_kpq[iq].shape[0] # Number of non-zeros
        ones = numpy.ones((nnz), dtype=numpy.complex128)
        rho_q = scipy.sparse.csc_matrix((ones, (self.rho_ikpq_kpq[iq], self.rho_ikpq_i[iq])),
            shape = (self.nbasis, self.nbasis) ,dtype=numpy.complex128 )
        return rho_q

    def scaled_density_operator_incore(self, transpose):
        """ Density operator as defined in Eq.(6) of PRB(75)245123
        Parameters
        ----------
        q : float
            a plane-wave vector
        Returns
        -------
        rho_q: float
            density operator
        """
        rho_ikpq_i = []
        rho_ikpq_kpq = []
        for (iq, q) in enumerate(self.qvecs):
            idxkpq_list_i =[]
            idxkpq_list_kpq =[]
            for i, k in enumerate(self.basis):
                kpq = k + q
                idxkpq = self.lookup_basis(kpq)
                if idxkpq is not None:
                    idxkpq_list_i += [i]
                    idxkpq_list_kpq += [idxkpq]
            rho_ikpq_i += [idxkpq_list_i]
            rho_ikpq_kpq += [idxkpq_list_kpq]

        for (iq, q) in enumerate(self.qvecs):
            rho_ikpq_i[iq]  = numpy.array(rho_ikpq_i[iq], dtype=numpy.int64)
            rho_ikpq_kpq[iq] = numpy.array(rho_ikpq_kpq[iq], dtype=numpy.int64)

        nq = len(self.qvecs)
        nnz = 0
        for iq in range(nq):
            nnz += rho_ikpq_kpq[iq].shape[0]

        col_index = []
        row_index = []

        values = []

        if (transpose):
            for iq in range(nq):
                qscaled = self.kfac * self.qvecs[iq]
                # Due to the HS transformation, we have to do pi / 2*vol as opposed to 2*pi / vol
                piovol = math.pi / (self.vol)
                factor = (piovol/numpy.dot(qscaled,qscaled))**0.5

                for (innz, kpq) in enumerate(rho_ikpq_kpq[iq]):
                    row_index += [rho_ikpq_kpq[iq][innz] + rho_ikpq_i[iq][innz]*self.nbasis]
                    col_index += [iq]
                    values += [factor]
        else:
            for iq in range(nq):
                qscaled = self.kfac * self.qvecs[iq]
                # Due to the HS transformation, we have to do pi / 2*vol as opposed to 2*pi / vol
                piovol = math.pi / (self.vol)
                factor = (piovol/numpy.dot(qscaled,qscaled))**0.5

                for (innz, kpq) in enumerate(rho_ikpq_kpq[iq]):
                    row_index += [rho_ikpq_kpq[iq][innz]*self.nbasis + rho_ikpq_i[iq][innz]]
                    col_index += [iq]
                    values += [factor]

        rho_q = scipy.sparse.csc_matrix((values, (row_index, col_index)),
            shape = (self.nbasis*self.nbasis, nq) ,dtype=numpy.complex128 )

        return rho_q

    def two_body_potentials_incore(self):
        """Calculatate A and B of Eq.(13) of PRB(75)245123 for a given plane-wave vector q
        Parameters
        ----------
        system :
            system class
        q : float
            a plane-wave vector
        Returns
        -------
        iA : numpy array
            Eq.(13a)
        iB : numpy array
            Eq.(13b)
        """
        # qscaled = self.kfac * self.qvecs

        # # Due to the HS transformation, we have to do pi / 2*vol as opposed to 2*pi / vol

        rho_q = self.scaled_density_operator_incore(False)
        rho_qH = self.scaled_density_operator_incore(True)

        iA = 1j * (rho_q + rho_qH)
        iB = - (rho_q - rho_qH)

        return (rho_q, iA, iB)

    def write_integrals(self, filename='hamil.h5'):
        dump_qmcpack_cholesky(self.H1, 2*scipy.sparse.csr_matrix(self.chol_vecs),
                              self.nelec, self.nbasis,
                              e0=0.0, filename=filename)

    def hijkl(self,i,j,k,l):
        """Compute <ij|kl> = (ik|jl) = 1/Omega * 4pi/(kk-ki)**2

        Checks for momentum conservation k_i + k_j = k_k + k_k, or
        k_k - k_i = k_j - k_l.

        Parameters
        ----------
        i, j, k, l : int
            Orbital indices for integral (ik|jl) = <ij|kl>.

        Returns
        -------
        integral : float
            (ik|jl)
        """
        q1 = self.basis[k] - self.basis[i]
        q2 = self.basis[j] - self.basis[l]
        if numpy.dot(q1,q1) > 1e-12 and numpy.dot(q1-q2,q1-q2) < 1e-12:
            return 1.0/self.vol * self.vq(self.kfac*q1)
        else:
            return 0.0

def unit_test():
    from numpy import linalg as LA
    from pauxy.estimators import ci as pauxyci
    # from pyscf import gto, scf, ao2mo, mcscf, fci, ci, cc, tdscf, gw, hci

    # # ecuts = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]

    # # eccsds = []
    # # for ecut in ecuts:

    # ecut = 4.0

    # inputs = {'nup':7,
    # 'ndown':7,
    # 'rs':1.0,
    # 'thermal':False,
    # 'ecut':ecut}
    # # -1.41489535 ecut = 1
    # # -1.41561583 ecut = 2
    # # -1.4161452950664903 ecut = 3
    # system = UEG(inputs, True)

    # mol = gto.M()
    # mol.nelectron = system.nup+system.ndown
    # mol.spin = system.nup - system.ndown
    # mol.incore_anyway = True
    # mol.verbose=0

    # M = system.nbasis

    # h1 = system.H1[0]
    # # eri = numpy.zeros((M,M,M,M))
    # # for i in range(M):
    # #     for k in range(M):
    # #         for j in range(M):
    # #             for l in range(M):
    # #                 eri[i,k,l,j] = system.hijkl(i,j,k,l)
    # # chol_vecs = system.chol_vecs.toarray()
    # # chol_vecs = chol_vecs.reshape((M, M, system.nchol))
    # # eri = 4*numpy.einsum("ikP, ljP->iklj",chol_vecs, numpy.conj(chol_vecs))
    # # eri = eri.real

    # eri_chol = 4 * system.chol_vecs.dot(system.chol_vecs.T)
    # eri_chol = eri_chol.toarray().reshape((M,M,M,M)).real
    # # eri_chol = numpy.einsum("ikjl->iklj",eri_chol)
    # # print(numpy.max(eri), numpy.max(eri_chol))
    # # eri_tmp = eri - eri_chol
    # # print(numpy.einsum("ijkl,ijkl->",eri_tmp,eri_tmp))
    # # exit()

    # eri = eri_chol

    # mol.symmetry = 0

    # mf = scf.RHF(mol)


    # mf.conv_tol = 1e-10

    # mf.get_hcore = lambda *args: h1
    # mf.get_ovlp = lambda *args: numpy.eye(M)
    # mf._eri = ao2mo.restore(4, eri, M)
    # mf.init_guess = '1e'
    # escf = mf.kernel()

    # # mci = fci.FCI(mol, mf.mo_coeff)
    # # mci = fci.addons.fix_spin_(mci, ss=0)
    # # mci.verbose = 4
    # # efci, civec = mci.kernel(nelec=system.nup+system.ndown, h1e=h1, eri=mf._eri, ecore = system.ecore, nroots=1)
    # # print(efci)
    # cisolver = hci.SCI(mol)
    # e, civec = cisolver.kernel(h1, mf._eri, M, system.nup+system.ndown, ecore = system.ecore, verbose=4)
    # print("ESCI = {}".format(e))

    # # cisolver = fci.selected_ci_spin0.SCI()
    # # cisolver.select_cutoff = 1e-4
    # # cisolver.verbose = 0
    # # e, fcivec = cisolver.kernel(h1, mf._eri, M, system.nup+system.ndown, ecore = system.ecore)
    # # print("ESCI = {}".format(e))

    # # mycc = cc.RCCSD(mf)
    # # mycc.verbose = 5
    # # mycc.kernel()
    # # eccsd = escf + mycc.e_corr + system.ecore
    # # eccsds += [eccsd]

    # # print(eccsds)

    # # (e0, ev), (d,oa,ob) = pauxyci.simple_fci(system, gen_dets=True)
    # # print(e0[0])
    inputs = {'nup':2,
              'ndown':2,
              'rs':1.0,
              'thermal':True,
              'ecut':3}
    system = UEG(inputs, True)

if __name__=="__main__":
    unit_test()
