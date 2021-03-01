import jax.numpy as jnp
import pytest

from gpjax.kernels import RBF, gram, cross_covariance, initialise
from gpjax.utils import I


@pytest.mark.parametrize("dim", [1, 2, 5])
def test_gram(dim):
    x = jnp.linspace(-1.0, 1.0, num=10).reshape(-1, 1)
    if dim > 1:
        x = jnp.hstack([x] * dim)
    kern = RBF()
    gram_matrix = gram(kern, x)
    assert gram_matrix.shape[0] == x.shape[0]
    assert gram_matrix.shape[0] == gram_matrix.shape[1]


@pytest.mark.parametrize('n1', [3, 10, 20])
@pytest.mark.parametrize('n2', [3, 10, 20])
def test_cross_covariance(n1, n2):
    x1 = jnp.linspace(-1., 1., num=n1).reshape(-1, 1)
    x2 = jnp.linspace(-1., 1., num=n2).reshape(-1, 1)
    kernel_matrix = cross_covariance(RBF(), x2, x1)
    assert kernel_matrix.shape == (n1, n2)


@pytest.mark.parametrize('dim', [1, 2, 5])
@pytest.mark.parametrize("ell, sigma", [(0.1, 0.1), (0.5, 0.1), (0.1, 0.5), (0.5, 0.5)])
def test_pos_def(dim, ell, sigma):
    n = 30
    x = jnp.linspace(0.0, 1.0, num=n).reshape(-1, 1)
    if dim > 1:
        x = jnp.hstack((x)*dim)
    kern = RBF()
    gram_matrix = sigma*gram(kern, x/ell)
    jitter_matrix = I(n) * 1e-6
    gram_matrix += jitter_matrix
    min_eig = jnp.linalg.eigvals(gram_matrix).min()
    assert min_eig > 0


@pytest.mark.parametrize('dim', [1, 2, 5, 10])
def test_initialisation(dim):
    params = initialise(RBF(ndims=dim))
    assert list(params.keys()) == ['lengthscale', 'variance']
    assert all(params['lengthscale'] == jnp.array([1.0]*dim))
    assert params['variance'] == jnp.array([1.0])
