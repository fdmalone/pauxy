import time
import numpy
try:
    from mpi4py import MPI
    mpi_sum = MPI.SUM
except ImportError:
    mpi_sum = None
import scipy.linalg

def local_energy_ueg(system, G):
    
    ke = numpy.sum(system.T[0] * G[0] + system.T[1] * G[1]) # kinetic energy
    
    Gkpq =  numpy.zeros((2,len(system.qvecs)), dtype=numpy.complex128)
    Gpmq =  numpy.zeros((2,len(system.qvecs)), dtype=numpy.complex128)
    Gprod = numpy.zeros((2,len(system.qvecs)), dtype=numpy.complex128)

    # print("Greens function = ")
    # print (G)

    ne = [system.nup, system.ndown]

#   Todo: make it work for different spin
    # kf = system.basis[0:ne[0]]
    kf = scipy.linalg.norm(system.basis[ne[0]])

    ikpq = []
    for (iq, q) in enumerate(system.qvecs):
        idxkpq =[]
        # for i, k in enumerate(kf):
        for i, k in enumerate(system.basis):
            kpq = k + q
            # if (scipy.linalg.norm(kpq) < kf):
            if (True):
                idx = system.lookup_basis(kpq)
                if idx is not None:
                    idxkpq += [(idx,i)]
        ikpq += [idxkpq]

    ipmq = []
    for (iq, q) in enumerate(system.qvecs):
        idxpmq =[]
        # for i, p in enumerate(kf):
        for i, p in enumerate(system.basis):
            pmq = p - q
            # if (scipy.linalg.norm(pmq) < kf):
            if (True):
                idx = system.lookup_basis(pmq)
                if idx is not None:
                    idxpmq += [(idx,i)]
        ipmq += [idxpmq]

    # essa = 0.0
    # essb = 0.0
    ess = [0.0, 0.0]
    eos = 0.0

    for s in [0, 1]:
        for (iq, q) in enumerate(system.qvecs):
            for (idxkpq, i) in ikpq[iq]:
                # summing over k
                Gkpq[s][iq] += G[s][idxkpq,i]

                for (j,idxpmq) in ipmq[iq]:
                    Gprod[s][iq] += G[s][idxkpq,j]*G[s][idxpmq,i]

        for (iq, q) in enumerate(system.qvecs):
            for (idxpmq, j) in ipmq[iq]:
                #summing over p
                Gpmq[s][iq] += G[s][idxpmq,j]

        fact = 1.0/(2.0*system.vol)

        tmp = numpy.multiply(Gkpq[s],Gpmq[s])-Gprod[s]

        ess[s] = fact * system.vqvec.dot(tmp)

    # essa = (1.0/(2.0*system.vol))*system.vqvec.dot(numpy.multiply(Gkpq[0],Gpmq[0])-Gprod[0])
    # essb = (1.0/(2.0*system.vol))*system.vqvec.dot(numpy.multiply(Gkpq[1],Gpmq[1])-Gprod[1])
    eos = (1.0/(2.0*system.vol))*system.vqvec.dot(Gkpq[0]*Gpmq[1]) + (1.0/(2.0*system.vol))*system.vqvec.dot(Gkpq[1]*Gpmq[0])

    pe = ess[0] + ess[1] + eos

    # G[0] = G[0].T
    # G[1] = G[1].T

    return (ke+pe, ke, pe)

def unit_test():
    from pauxy.systems.ueg import UEG
    import numpy as np
    inputs = {'nup':7, 
    'ndown':7,
    'rs':1.0,
    'ecut':1.0}
    system = UEG(inputs, True)
    nbsf = system.nbasis
    Pa = np.zeros([nbsf,nbsf])
    Pb = np.zeros([nbsf,nbsf])
    na = system.nup
    nb = system.ndown
    for i in range(na):
        Pa[i,i] = 1.0
    for i in range(nb):
        Pb[i,i] = 1.0
    P = [Pa, Pb]

    # print (system.basis)

    etot, ekin, epot = local_energy_ueg(system, P)
# Number of spin-up electrons = 7
# Number of spin-down electrons = 7
# Number of plane waves = 19
# Finished setting up Generic system object.
# ((13.603557335564194+0j), 15.692780148560844, (-2.0892228129966512+0j))
############
# Number of spin-up electrons = 7
# Number of spin-down electrons = 0
# Number of plane waves = 19
# Finished setting up Generic system object.
# ((11.139239958058056+0j), 12.455367858065586, (-1.3161279000075299+0j))
    print (etot, ekin, epot)

if __name__=="__main__":
    unit_test()
