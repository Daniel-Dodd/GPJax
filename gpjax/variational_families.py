import abc
from typing import Callable, Dict, Optional, Any
import distrax as dx
import jax.numpy as jnp
import jax.scipy as jsp
import tensorflow_probability.substrates.jax.bijectors as tfb
from chex import dataclass

from .config import Identity, Softplus, add_parameter, get_defaults
from .types import Array
from .utils import I, concat_dictionaries
from .kernels import cross_covariance, gram
from .gps import Prior

DEFAULT_JITTER = get_defaults()["jitter"]

Diagonal = dx.Lambda(
    forward=lambda x: jnp.diagflat(x), inverse=lambda x: jnp.diagonal(x)
)

FillDiagonal = dx.Chain([Diagonal, Softplus])
FillTriangular = dx.Chain([tfb.FillTriangular()])


@dataclass
class AbstractVariationalFamily:
    """Abstract base class used to represent families of distributions that can be used within variational inference."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """For a given set of parameters, compute the latent function's prediction under the variational approximation."""
        return self.predict(*args, **kwargs)

    @property
    @abc.abstractmethod
    def params(self) -> Dict:
        """The parameters of the distribution. For example, the multivariate Gaussian would return a mean vector and covariance matrix."""
        raise NotImplementedError

    @abc.abstractmethod
    def predict(self, *args: Any, **kwargs: Any) -> dx.Distribution:
        """Predict the GP's output given the input."""
        raise NotImplementedError


@dataclass
class VariationalGaussian(AbstractVariationalFamily):
    """The variational Gaussian family of probability distributions.
    
    The variational family is q(f(·)) = ∫ p(f(·)|u) q(u) du, where u = f(z) are the function values at the inducing inputs z
    and the distribution over the inducing inputs is q(u) = N(μ, S). We parameterise this over μ and sqrt with S = sqrt sqrtᵀ.

    """
    prior: Prior
    inducing_inputs: Array
    name: str = "Gaussian"
    variational_mean: Optional[Array] = None
    variational_root_covariance: Optional[Array] = None
    diag: Optional[bool] = False
    jitter: Optional[float] = DEFAULT_JITTER

    def __post_init__(self):
        """Initialise the variational Gaussian distribution."""
        self.num_inducing = self.inducing_inputs.shape[0]
        add_parameter("inducing_inputs", Identity)

        m = self.num_inducing

        if self.variational_mean is None:
            self.variational_mean = jnp.zeros((m, 1))
            add_parameter("variational_mean", Identity)

        if self.variational_root_covariance is None:
            self.variational_root_covariance = I(m)
            if self.diag:
                add_parameter("variational_root_covariance", FillDiagonal)
            else:
                add_parameter("variational_root_covariance", FillTriangular)

    @property
    def params(self) -> Dict:
        """Return the variational mean vector, variational root covariance matrix, and inducing input vector that parameterise the variational Gaussian distribution."""
        return concat_dictionaries(
            self.prior.params, {
            "variational_family": {
                "inducing_inputs": self.inducing_inputs,
                "variational_mean": self.variational_mean,
                "variational_root_covariance": self.variational_root_covariance}
                }
        )

    def prior_kl(self, params: Dict) -> Array:
        """Compute the KL-divergence between our variational approximation and the Gaussian process prior.

        For this variational family, we have KL[q(f(·))||p(·)] = KL[q(u)||p(u)] = KL[ N(μ, S) || N(μz, Kzz) ],
        where u = f(z) and z are the inducing inputs. 

        Args:
            params (Dict): The parameters at which our variational distribution and GP prior are to be evaluated.

        Returns:
            Array: The KL-divergence between our variational approximation and the GP prior.
        """
        mu = params["variational_family"]["variational_mean"]
        sqrt = params["variational_family"]["variational_root_covariance"]
        m = self.num_inducing
        z = params["variational_family"]["inducing_inputs"]
        μz = self.prior.mean_function(z, params["mean_function"])
        Kzz = gram(self.prior.kernel, z, params["kernel"])
        Kzz += I(m) * self.jitter
        Lz = jnp.linalg.cholesky(Kzz)

        qu = dx.MultivariateNormalTri(jnp.atleast_1d(mu.squeeze()), sqrt)
        pu = dx.MultivariateNormalTri(jnp.atleast_1d(μz.squeeze()), Lz)

        return qu.kl_divergence(pu)

    def predict(self, params: dict) -> Callable[[Array], dx.Distribution]:
        """Compute the predictive distribution of the GP at the test inputs t.

        This is the integral q(f(t)) = ∫ p(f(t)|u) q(u) du, which can be computed in closed form as:

            N[f(t); μt + Ktz Kzz⁻¹ (μ - μz),  Ktt - Ktz Kzz⁻¹ Kzt + Ktz Kzz⁻¹ S Kzz⁻¹ Kzt ].

        Args:
            params (dict): The set of parameters that are to be used to parameterise our variational approximation and GP.

        Returns:
            Callable[[Array], dx.Distribution]: A function that accepts a set of test points and will return the predictive distribution at those points.
        """
        mu = params["variational_family"]["variational_mean"]
        sqrt = params["variational_family"]["variational_root_covariance"]
        z = params["variational_family"]["inducing_inputs"]
        m = self.num_inducing
        
        Kzz = gram(self.prior.kernel, z, params["kernel"])
        Kzz += I(m) * self.jitter
        Lz = jnp.linalg.cholesky(Kzz)
        μz = self.prior.mean_function(z, params["mean_function"])

        def predict_fn(test_inputs: Array) -> dx.Distribution:
            t = test_inputs
            n_test = t.shape[0]
            Ktt = gram(self.prior.kernel, t, params["kernel"])
            Kzt = cross_covariance(self.prior.kernel, z, t, params["kernel"])
            μt = self.prior.mean_function(t, params["mean_function"])

            # Lz⁻¹ Kzt
            Lz_inv_Kzt = jsp.linalg.solve_triangular(Lz, Kzt, lower=True)

            # Kzz⁻¹ Kzt
            Kzz_inv_Kzt = jsp.linalg.solve_triangular(Lz.T, Lz_inv_Kzt, lower=False)

            # Ktz Kzz⁻¹ sqrt
            Ktz_Kzz_inv_sqrt = jnp.matmul(Kzz_inv_Kzt.T, sqrt)
            
            # μt + Ktz Kzz⁻¹ (μ - μz)
            mean = μt + jnp.matmul(Kzz_inv_Kzt.T, mu - μz)

            # Ktt  -  Ktz Kzz⁻¹ Kzt  +  Ktz Kzz⁻¹ S Kzz⁻¹ Kzt  [recall S = sqrt sqrtᵀ]
            covariance = Ktt - jnp.matmul(Lz_inv_Kzt.T, Lz_inv_Kzt) + jnp.matmul(Ktz_Kzz_inv_sqrt, Ktz_Kzz_inv_sqrt.T)
            covariance += I(n_test) * self.jitter

            return dx.MultivariateNormalFullCovariance(
                jnp.atleast_1d(mean.squeeze()), covariance
            )

        return predict_fn


@dataclass
class WhitenedVariationalGaussian(VariationalGaussian):
    """The whitened variational Gaussian family of probability distributions.

    The variational family is q(f(·)) = ∫ p(f(·)|u) q(u) du, where u = f(z) are the function values at the inducing inputs z
    and the distribution over the inducing inputs is q(u) = N(Lz μ + mz, Lz S). We parameterise this over μ and sqrt with S = sqrt sqrtᵀ.
    
    """

    def prior_kl(self, params: Dict) -> Array:
        """Compute the KL-divergence between our variational approximation and the Gaussian process prior.

        For this variational family, we have KL[q(f(·))||p(·)] = KL[q(u)||p(u)] = KL[N(μ, S)||N(0, I)].

        Args:
            params (Dict): The parameters at which our variational distribution and GP prior are to be evaluated.

        Returns:
            Array: The KL-divergence between our variational approximation and the GP prior.
        """
        mu = params["variational_family"]["variational_mean"]
        sqrt = params["variational_family"]["variational_root_covariance"]
        m = self.num_inducing

        qu = dx.MultivariateNormalTri(jnp.atleast_1d(mu.squeeze()), sqrt)
        pu = dx.MultivariateNormalDiag(jnp.zeros(m))

        return qu.kl_divergence(pu)

    def predict(self, params: dict) -> Callable[[Array], dx.Distribution]:
        """Compute the predictive distribution of the GP at the test inputs t.

        This is the integral q(f(t)) = ∫ p(f(t)|u) q(u) du, which can be computed in closed form as
          
            N[f(t); μt  +  Ktz Lz⁻ᵀ μ,  Ktt  -  Ktz Kzz⁻¹ Kzt  +  Ktz Lz⁻ᵀ S Lz⁻¹ Kzt].

        Args:
            params (dict): The set of parameters that are to be used to parameterise our variational approximation and GP.

        Returns:
            Callable[[Array], dx.Distribution]: A function that accepts a set of test points and will return the predictive distribution at those points.
        """
        mu = params["variational_family"]["variational_mean"]
        sqrt = params["variational_family"]["variational_root_covariance"]
        z = params["variational_family"]["inducing_inputs"]
        m = self.num_inducing

        Kzz = gram(self.prior.kernel, z, params["kernel"])
        Kzz += I(m) * self.jitter
        Lz = jnp.linalg.cholesky(Kzz)

        def predict_fn(test_inputs: Array) -> dx.Distribution:
            t = test_inputs
            n_test = t.shape[0]
            Ktt = gram(self.prior.kernel, t, params["kernel"])
            Kzt = cross_covariance(self.prior.kernel, z, t, params["kernel"])
            μt = self.prior.mean_function(t, params["mean_function"])

            # Lz⁻¹ Kzt
            Lz_inv_Kzt = jsp.linalg.solve_triangular(Lz, Kzt, lower=True)

            # Ktz Lz⁻ᵀ sqrt
            Ktz_Lz_invT_sqrt = jnp.matmul(Lz_inv_Kzt.T, sqrt)
            
            # μt  +  Ktz Lz⁻ᵀ μ
            mean = μt + jnp.matmul(Lz_inv_Kzt.T, mu)

           # Ktt  -  Ktz Kzz⁻¹ Kzt  +  Ktz Lz⁻ᵀ S Lz⁻¹ Kzt  [recall S = sqrt sqrtᵀ]
            covariance = Ktt - jnp.matmul(Lz_inv_Kzt.T, Lz_inv_Kzt) + jnp.matmul(Ktz_Lz_invT_sqrt, Ktz_Lz_invT_sqrt.T)
            covariance += I(n_test) * self.jitter

            return dx.MultivariateNormalFullCovariance(
                jnp.atleast_1d(mean.squeeze()), covariance
            )

        return predict_fn