"""
Contains classes for different geometry optimization algorithms.

TODO

Algorithms implemented by Michele Ceriotti and Benjamin Helfrecht, 2015
"""

# This file is part of i-PI.
# i-PI Copyright (C) 2014-2015 i-PI developers
# See the "licenses" directory for full license information.


import numpy as np
import time

from ipi.engine.motion import Motion
from ipi.utils.depend import dstrip, dobject
from ipi.utils.softexit import softexit
from ipi.utils.mintools import min_brent, BFGS, BFGSTRM, L_BFGS
from ipi.utils.messages import verbosity, info


__all__ = ['CellopMotion']


class CellopMotion(Motion):
    """Geometry optimization class.

    Attributes:
        mode: minimization algorithm to use
        biggest_step: max allowed step size for BFGS/L-BFGS
        old_force: force on previous step
        old_direction: move direction on previous step
        invhessian_bfgs: stored inverse Hessian matrix for BFGS
        hessian_trm: stored  Hessian matrix for trm
        ls_options:
        {tolerance: energy tolerance for exiting minimization algorithm
        iter: maximum number of allowed iterations for minimization algorithm for each MD step
        step: initial step size for steepest descent and conjugate gradient
        adaptive: T/F adaptive step size for steepest descent and conjugate
                gradient}
        tolerances:
        {energy: change in energy tolerance for ending minimization
        force: force/change in force tolerance foe ending minimization
        position: change in position tolerance for ending minimization}
        corrections_lbfgs: number of corrections to be stored for L-BFGS
        scale_lbfgs: Scale choice for the initial hessian.
        qlist_lbfgs: list of previous positions (x_n+1 - x_n) for L-BFGS. Number of entries = corrections_lbfgs
        glist_lbfgs: list of previous gradients (g_n+1 - g_n) for L-BFGS. Number of entries = corrections_lbfgs
    """

    def __init__(self, fixcom=False, fixatoms=None,
                 mode="lbfgs",
                 biggest_step=100.0,
                 old_pos=np.zeros(0, float),
                 old_pot=np.zeros(0, float),
                 old_force=np.zeros(0, float),
                 old_direction=np.zeros(0, float),
                 invhessian_bfgs=np.eye(0, 0, 0, float),
                 hessian_trm=np.eye(0, 0, 0, float),
                 tr_trm=np.zeros(0, float),
                 ls_options={"tolerance": 1, "iter": 100, "step": 1e-3, "adaptive": 1.0},
                 tolerances={"energy": 1e-7, "force": 1e-4, "position": 1e-4},
                 corrections_lbfgs=5,
                 scale_lbfgs=1,
                 qlist_lbfgs=np.zeros(0, float),
                 glist_lbfgs=np.zeros(0, float)):
        """Initialises CellopMotion.

        Args:
           fixcom: An optional boolean which decides whether the centre of mass
              motion will be constrained or not. Defaults to False.
        """
        if len(fixatoms) > 0:
            raise ValueError("The optimization algorithm with fixatoms is not implemented. "
                             "We stop here. Comment this line and continue only if you know what you are doing")

        super(CellopMotion, self).__init__(fixcom=fixcom, fixatoms=fixatoms)

        # Optimization Options

        self.mode = mode
        self.big_step = biggest_step
        self.tolerances = tolerances
        self.ls_options = ls_options

        #
        self.old_x = old_pos
        self.old_u = old_pot
        self.old_f = old_force
        self.d = old_direction

        # Classes for minimization routines and specific attributes
        if self.mode == "bfgs":
            self.invhessian = invhessian_bfgs
            self.optimizer = BFGSOptimizer()
        elif self.mode == "bfgstrm":
            self.tr = tr_trm
            self.hessian = hessian_trm
            self.optimizer = BFGSTRMOptimizer()
        elif self.mode == "lbfgs":
            self.corrections = corrections_lbfgs
            self.scale = scale_lbfgs
            self.qlist = qlist_lbfgs
            self.glist = glist_lbfgs
            self.optimizer = LBFGSOptimizer()
        elif self.mode == "sd":
            self.optimizer = SDOptimizer()
        elif self.mode == "cg":
            self.optimizer = CGOptimizer()
        else:
            self.optimizer = DummyOptimizer()

    def bind(self, ens, beads, nm, cell, bforce, prng):
        """Binds beads, cell, bforce and prng to CellopMotion

            Args:
            beads: The beads object from whcih the bead positions are taken.
            nm: A normal modes object used to do the normal modes transformation.
            cell: The cell object from which the system box is taken.
            bforce: The forcefield object from which the force and virial are taken.
            prng: The random number generator object which controls random number generation.
        """

        super(CellopMotion, self).bind(ens, beads, nm, cell, bforce, prng)
        # Binds optimizer
        self.optimizer.bind(self)

    def step(self, step=None):
        self.optimizer.step(step)

class GradientMapper(object):

    """Creation of the multi-dimensional function that will be minimized.
    Used in the BFGS and L-BFGS minimizers.

    Attributes:
        dbeads:   copy of the bead object
        dcell:   copy of the cell object
        dforces: copy of the forces object
    """

    def __init__(self,h=None):
        self.fcount = 0
        """Initialises base cell class.

        Args:
           h: Optional array giving the initial lattice vector matrix. The
              reference cell matrix is set equal to this. Must be an upper
              triangular 3*3 matrix. Defaults to a 3*3 zeroes matrix.
        """

        if h is None:
            h = np.zeros((3, 3), float)

        dself = dd(self)  # gets a direct-access view to self

        dself.h = depend_array(name='h', value=h)
        dself.ih = depend_array(name="ih", value=np.zeros((3, 3), float),
                                func=self.get_ih, dependencies=[dself.h])
        dself.V = depend_value(name='V', func=self.get_volume,
                               dependencies=[dself.h])
        pass

    def bind(self, dumop, atoms, cell, ff):
        self.dbeads = dumop.beads.copy()
        self.dcell = dumop.cell.copy()
        self.dforces = dumop.forces.copy(self.dbeads, self.dcell)
        self.h0 = dstrip(self.dcell.h).copy()
        self.ih0 = dstrip(self.dcell.ih).copy()
        global fbuid  # assign a unique identifier to each forcebead object
        with self._threadlock:

            self.uid = fbuid
            fbuid += 1

            # stores a reference to the atoms and cell we are computing forces for
        self.atoms = atoms
        self.cell = cell
        self.ff = ff
        dself = dd(self)

            # ufv depends on the atomic positions and on the cell
        dself.ufvx.add_dependency(dd(self.atoms).q)
        dself.ufvx.add_dependency(dd(self.cell).h)
        dcopy(dself.f, dself.fx)
        dcopy(dself.f, dself.fy)
        dcopy(dself.f, dself.fz)


    def __call__(self, x):
        """computes energy and gradient for optimization step"""

        self.fcount += 1
        self.dbeads.q = x
        self.h0

        self.pext = 0.0 #  for zero external pressure
        self.strain = (np.dot(dstrip(self.dcell.h),self.ih0) - np.eye(3)).flatten()
        self.metric = np.dot(self.dcell.h.T, self.dcell.h)

        f  = self.dforces.f[0]
        nat = len(f) / 3
        f = f.reshape((nat,3))
        sf = np.dot()


        # Defines the effective energy
        e = self.dforces.pot   # Energy
        pV = self.pext * self.dcell.V
        e = e + pV

        # Defines the effective gradient
        g = np.zeros(len(self.dforces.f) + 9)   # Gradient contains 3N + 9 components
        g[0:9] = - np.dot((self.dforces.vir + np.eye(3) * pV), np.eye(3) + (self.strain).T).flatten()
        g[9:] = self.metric * sf


        #entp = self.dforces.pot + (np.trace((self.dforces.vir) / 3.0))  # enthalpy

        ggT =
        print("LIST OF PRINTED PROPS")
        print(self.dforces.pot / self.dbeads.nbeads, np.trace((self.dforces.vir) / (3.0 * self.dcell.V)),self.tensor2vec((self.dforces.vir) / self.dcell.V))
        return e, g

    def tensor2vec(self, tensor):
        """Takes a 3*3 symmetric tensor and returns it as a 1D array,
        containing the elements [xx, yy, zz, xy, xz, yz].
        """
        return np.array([tensor[0, 0], tensor[1, 1], tensor[2, 2], tensor[0, 1], tensor[0, 2], tensor[1, 2]])

    #def abs_to_scaled(self):
        #fractional = np.linalg.solve(self.dcell.h.T, self.dbeads.q.T).T
        #return fractional

    #def scaled_forces(self):
        #scaledForce = np.linalg.solve(self.dcell.h.T, self.dforces.f.T).T
        #return scaledForce


class DummyOptimizer(dobject):
    """ Dummy class for all optimization classes """

    def __init__(self):
        """initialises object for LineMapper (1-d function) and for GradientMapper (multi-dimensional function) """


        self.gm = GradientMapper()

    def step(self, step=None):
        """Dummy simulation time step which does nothing."""
        pass

   def scaled_to_abs(self):
       absposition = np.dot(self.dcell.h.T,abs_to_scaled(self).T).T
       return absposition

   def abs_to_scaled(self):
       fractional = np.linalg.solve(self.dcell.h.T, self.dbeads.q.T).T
       return fractional

    def bind(self, geop):
        """
        bind optimization options and call bind function of LineMapper and GradientMapper (get beads, cell,forces)
        check whether force size, direction size and inverse Hessian size from previous step match system size
        """
        self.beads = geop.beads
        self.cell = geop.cell
        self.forces = geop.forces
        self.fixcom = geop.fixcom
        self.fixatoms = geop.fixatoms
        self.p_ext = geop.p_ext # should come from the i-pi input

        self.mode = geop.mode
        self.tolerances = geop.tolerances

        # Check for very tight tolerances

        if self.tolerances["position"] < 1e-7:
            raise ValueError("The position tolerance is too small for any typical calculation. "
                             "We stop here. Comment this line and continue only if you know what you are doing")
        if self.tolerances["force"] < 1e-7:
            raise ValueError("The force tolerance is too small for any typical calculation. "
                             "We stop here. Comment this line and continue only if you know what you are doing")
        if self.tolerances["energy"] < 1e-10:
            raise ValueError("The energy tolerance is too small for any typical calculation. "
                             "We stop here. Comment this line and continue only if you know what you are doing")

        # The resize action must be done before the bind
        if geop.old_x.size != self.beads.q.size:
            if geop.old_x.size == 0:
                geop.old_x = np.zeros((self.beads.nbeads, 3 * self.beads.natoms), float)
            else:
                raise ValueError("Conjugate gradient force size does not match system size")
        if geop.old_u.size != 1:
            if geop.old_u.size == 0:
                geop.old_u = np.zeros(1, float)
            else:
                raise ValueError("Conjugate gradient force size does not match system size")
        if geop.old_f.size != self.beads.q.size:
            if geop.old_f.size == 0:
                geop.old_f = np.zeros((self.beads.nbeads, 3 * self.beads.natoms), float)
            else:
                raise ValueError("Conjugate gradient force size does not match system size")
        if geop.d.size != self.beads.q.size:
            if geop.d.size == 0:
                geop.d = np.zeros((self.beads.nbeads, 3 * self.beads.natoms), float)
            else:
                raise ValueError("Conjugate gradient direction size does not match system size")

        self.old_x = geop.old_x
        self.old_u = geop.old_u
        self.old_f = geop.old_f
        self.d = geop.d

    def exitstep(self, fx, u0, x):
        """ Exits the simulation step. Computes time, checks for convergence. """

        info(" @GEOP: Updating bead positions", verbosity.debug)
        self.qtime += time.time()

        if len(self.fixatoms) > 0:
            ftmp = self.forces.f.copy()
            for dqb in ftmp:
                dqb[self.fixatoms * 3] = 0.0
                dqb[self.fixatoms * 3 + 1] = 0.0
                dqb[self.fixatoms * 3 + 2] = 0.0
            fmax = np.amax(np.absolute(ftmp))
        else:
            fmax = np.amax(np.absolute(self.forces.f))

        e = np.absolute((fx - u0) / self.beads.natoms)
        info("@GEOP", verbosity.medium)
        self.tolerances["position"]
        info("   Current energy             %e" % (fx))
        info("   Position displacement      %e  Tolerance %e" % (x, self.tolerances["position"]), verbosity.medium)
        info("   Max force component        %e  Tolerance %e" % (fmax, self.tolerances["force"]), verbosity.medium)
        info("   Energy difference per atom %e  Tolerance %e" % (e, self.tolerances["energy"]), verbosity.medium)

        if (np.linalg.norm(self.forces.f.flatten() - self.old_f.flatten()) <= 1e-20):
            softexit.trigger("Something went wrong, the forces are not changing anymore."
                             " This could be due to an overly small tolerance threshold "
                             "that makes no physical sense. Please check if you are able "
                             "to reach such accuracy with your force evaluation"
                             " code (client).")

        if (np.absolute((fx - u0) / self.beads.natoms) <= self.tolerances["energy"])   \
                and (fmax <= self.tolerances["force"])  \
                and (x <= self.tolerances["position"]):
            softexit.trigger("Geometry optimization converged. Exiting simulation")


class BFGSOptimizer(DummyOptimizer):
    """ BFGS Minimization """

    def bind(self, geop):
        # call bind function from DummyOptimizer
        super(BFGSOptimizer, self).bind(geop)

        if geop.invhessian.size != (self.beads.q.size * self.beads.q.size):
            if geop.invhessian.size == 0:
                geop.invhessian = np.eye(self.beads.q.size, self.beads.q.size, 0, float)
            else:
                raise ValueError("Inverse Hessian size does not match system size")

        self.invhessian = geop.invhessian
        self.gm.bind(self)
        self.big_step = geop.big_step
        self.ls_options = geop.ls_options

    def step(self, step=None):
        """ Does one simulation time step.
            Attributes:
            qtime: The time taken in updating the positions.
        """

        self.qtime = -time.time()
        info("\nMD STEP %d" % step, verbosity.debug)

        if step == 0:
            info(" @GEOP: Initializing BFGS", verbosity.debug)
            self.d += dstrip(self.forces.f) / np.sqrt(np.dot(self.forces.f.flatten(), self.forces.f.flatten()))

            if len(self.fixatoms) > 0:
                for dqb in self.d:
                    dqb[self.fixatoms * 3] = 0.0
                    dqb[self.fixatoms * 3 + 1] = 0.0
                    dqb[self.fixatoms * 3 + 2] = 0.0

        self.old_x[:] = self.beads.q
        self.old_u[:] = self.forces.pot
        self.old_f[:] = self.forces.f

        if len(self.fixatoms) > 0:
            for dqb in self.old_f:
                dqb[self.fixatoms * 3] = 0.0
                dqb[self.fixatoms * 3 + 1] = 0.0
                dqb[self.fixatoms * 3 + 2] = 0.0

        fdf0 = (self.old_u, -self.old_f)

        # Do one iteration of BFGS
        # The invhessian and the directions are updated inside.
        BFGS(self.old_x, self.d, self.gm, fdf0, self.invhessian, self.big_step,
             self.ls_options["tolerance"] * self.tolerances["energy"], self.ls_options["iter"])

        info("   Number of force calls: %d" % (self.gm.fcount)); self.gm.fcount = 0
        # Update positions and forces
        self.beads.q = self.gm.dbeads.q
        self.forces.transfer_forces(self.gm.dforces)  # This forces the update of the forces
        print("this is cellop")
        # Exit simulation step
        d_x_max = np.amax(np.absolute(np.subtract(self.beads.q, self.old_x)))
        self.exitstep(self.forces.pot, self.old_u, d_x_max)