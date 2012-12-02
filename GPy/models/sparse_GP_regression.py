# Copyright (c) 2012, GPy authors (see AUTHORS.txt).
# Licensed under the BSD 3-clause license (see LICENSE.txt)


import numpy as np
import pylab as pb
from ..util.linalg import mdot, jitchol, chol_inv, pdinv
from ..util.plot import gpplot
from .. import kern
from ..inference.likelihoods import likelihood
from GP_regression import GP_regression

#Still TODO:
# make use of slices properly (kernel can now do this)
# enable heteroscedatic noise (kernel will need to compute psi2 as a (NxMxM) array)

class sparse_GP_regression(GP_regression):
    """
    Variational sparse GP model (Regression)

    :param X: inputs
    :type X: np.ndarray (N x Q)
    :param Y: observed data
    :type Y: np.ndarray of observations (N x D)
    :param kernel : the kernel/covariance function. See link kernels
    :type kernel: a GPy kernel
    :param Z: inducing inputs (optional, see note)
    :type Z: np.ndarray (M x Q) | None
    :param Zslices: slices for the inducing inputs (see slicing TODO: link)
    :param M : Number of inducing points (optional, default 10. Ignored if Z is not None)
    :type M: int
    :param beta: noise precision. TODO> ignore beta if doing EP
    :type beta: float
    :param normalize_(X|Y) : whether to normalize the data before computing (predictions will be in original scales)
    :type normalize_(X|Y): bool
    """

    def __init__(self,X,Y,kernel=None, beta=100., Z=None,Zslices=None,M=10,normalize_X=False,normalize_Y=False):
        self.beta = beta
        if Z is None:
            self.Z = np.random.permutation(X.copy())[:M]
            self.M = M
        else:
            assert Z.shape[1]==X.shape[1]
            self.Z = Z
            self.M = Z.shape[1]

        GP_regression.__init__(self, X, Y, kernel=kernel, normalize_X=normalize_X, normalize_Y=normalize_Y)
        self.trYYT = np.sum(np.square(self.Y))

    def set_param(self, p):
        self.Z = p[:self.M*self.Q].reshape(self.M, self.Q)
        self.beta = p[self.M*self.Q]
        self.kern.set_param(p[self.Z.size + 1:])
        self.beta2 = self.beta**2
        self._compute_kernel_matrices()
        self._computations()

    def _compute_kernel_matrices(self):
        # kernel computations, using BGPLVM notation
        #TODO: the following can be switched out in the case of uncertain inputs (or the BGPLVM!)
        #TODO: slices for psi statistics (easy enough)

        self.Kmm = self.kern.K(self.Z)
        self.psi0 = self.kern.Kdiag(self.X,slices=self.Xslices).sum()
        self.psi1 = self.kern.K(self.Z,self.X)
        self.psi2 = np.dot(self.psi1,self.psi1.T)

    def _computations(self):
        # TODO find routine to multiply triangular matrices
        self.V = self.beta*self.Y
        self.psi1V = np.dot(self.psi1, self.V)
        self.psi1VVpsi1 = np.dot(self.psi1V, self.psi1V.T)
        self.Lm = jitchol(self.Kmm)
        self.Lmi = chol_inv(self.Lm)
        self.Kmmi = np.dot(self.Lmi.T, self.Lmi)
        self.A = mdot(self.Lmi, self.psi2, self.Lmi.T)
        self.B = np.eye(self.M) + self.beta * self.A
        self.LB = jitchol(self.B)
        self.LBi = chol_inv(self.LB)
        self.Bi = np.dot(self.LBi.T, self.LBi)
        self.LLambdai = np.dot(self.LBi, self.Lmi)
        self.trace_K = self.psi0 - np.trace(self.A)
        self.LBL_inv = mdot(self.Lmi.T, self.Bi, self.Lmi)
        self.C = mdot(self.LLambdai, self.psi1V)
        self.G =  mdot(self.LBL_inv, self.psi1VVpsi1, self.LBL_inv.T)

        # Compute dL_dpsi
        self.dL_dpsi0 = - 0.5 * self.D * self.beta * np.ones(self.N)
        dC_dpsi1 = (self.LLambdai.T[:,:, None, None] * self.V)
        self.dL_dpsi1 = (dC_dpsi1*self.C[None,:,None,:]).sum(1).sum(-1)
        self.dL_dpsi2 = - 0.5 * self.beta * (self.D*(self.LBL_inv - self.Kmmi) + self.G)

        # Compute dL_dKmm
        self.dL_dKmm = -0.5 * self.beta * self.D * mdot(self.Lmi.T, self.A, self.Lmi) # dB
        self.dL_dKmm += -0.5 * self.D * (- self.LBL_inv - 2.*self.beta*mdot(self.LBL_inv, self.psi2, self.Kmmi) + self.Kmmi) # dC
        self.dL_dKmm +=  np.dot(np.dot(self.G,self.beta*self.psi2) - np.dot(self.LBL_inv, self.psi1VVpsi1), self.Kmmi) + 0.5*self.G # dE

    def get_param(self):
        return np.hstack([self.Z.flatten(),self.beta,self.kern.extract_param()])

    def get_param_names(self):
        return sum([['iip_%i_%i'%(i,j) for i in range(self.Z.shape[0])] for j in range(self.Z.shape[1])],[]) + ['noise_precision']+self.kern.extract_param_names()

    def log_likelihood(self):
        """
        Compute the (lower bound on the) log marginal likelihood
        """
        A = -0.5*self.N*self.D*(np.log(2.*np.pi) - np.log(self.beta))
        B = -0.5*self.beta*self.D*self.trace_K
        C = -self.D * np.sum(np.log(np.diag(self.LB)))
        D = -0.5*self.beta*self.trYYT
        E = +0.5*np.sum(self.psi1VVpsi1 * self.LBL_inv)
        return A+B+C+D+E

    def dL_dbeta(self):
        """
        Compute the gradient of the log likelihood wrt beta.
        TODO: suport heteroscedatic noise
        """

        dA_dbeta =   0.5 * self.N*self.D/self.beta
        dB_dbeta = - 0.5 * self.D * self.trace_K
        dC_dbeta = - 0.5 * self.D * np.sum(self.Bi*self.A)
        dD_dbeta = - 0.5 * self.trYYT
        tmp = mdot(self.LBi.T, self.LLambdai, self.psi1V)
        dE_dbeta = np.sum(np.square(self.C))/self.beta - 0.5 * np.sum(self.A * np.dot(tmp, tmp.T))

        return np.squeeze(dA_dbeta + dB_dbeta + dC_dbeta + dD_dbeta + dE_dbeta)

    def dL_dtheta(self):
        """
        Compute and return the derivative of the log marginal likelihood wrt the parameters of the kernel
        """
        #re-cast computations in psi2 back to psi1:
        dL_dpsi1 = self.dL_dpsi1 + 2.*np.dot(self.dL_dpsi2,self.psi1)

        dL_dtheta = self.kern.dK_dtheta(self.dL_dKmm,self.Z)
        dL_dtheta += self.kern.dK_dtheta(dL_dpsi1,self.Z,self.X)
        dL_dtheta += self.kern.dKdiag_dtheta(self.dL_dpsi0, self.X)

        return dL_dtheta

    def dL_dZ(self):
        #re-cast computations in psi2 back to psi1:
        dL_dpsi1 = self.dL_dpsi1 + 2.*np.dot(self.dL_dpsi2,self.psi1)

        dL_dZ = 2.*self.kern.dK_dX(self.dL_dKmm,self.Z,)#factor of two becase of vertical and horizontal 'stripes' in dKmm_dZ
        dL_dZ += self.kern.dK_dX(dL_dpsi1,self.Z,self.X)
        return dL_dZ

    def log_likelihood_gradients(self):
        return np.hstack([self.dL_dZ().flatten(), self.dL_dbeta(), self.dL_dtheta()])

    def _raw_predict(self, Xnew, slices):
        """Internal helper function for making predictions, does not account for normalisation"""

        Kx = self.kern.K(self.Z, Xnew)
        Kxx = self.kern.K(Xnew)

        mu = mdot(Kx.T, self.LBL_inv, self.psi1V)
        var = Kxx - mdot(Kx.T, (self.Kmmi - self.LBL_inv), Kx) + np.eye(Xnew.shape[0])/self.beta # TODO: This beta doesn't belong here in the EP case.
        return mu,var

    def plot(self, *args, **kwargs):
        """
        Plot the fitted model: just call the GP_regression plot function and then add inducing inputs
        """
        GP_regression.plot(self,*args,**kwargs)
        if self.Q==1:
            pb.plot(self.Z,self.Z*0+pb.ylim()[0],'k|',mew=1.5,markersize=12)
        if self.Q==2:
            pb.plot(self.Z[:,0],self.Z[:,1],'wo')
