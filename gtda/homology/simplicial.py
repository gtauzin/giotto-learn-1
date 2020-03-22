"""Persistent homology on point clouds or finite metric spaces."""
# License: GNU AGPLv3

from numbers import Real
from types import FunctionType

import numpy as np
from joblib import Parallel, delayed
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.metrics.pairwise import pairwise_distances
from sklearn.utils.validation import check_array, check_is_fitted

from ..base import PlotterMixin
from ._utils import _postprocess_diagrams
from ._subsamplings import SUBSAMPLING_FUNCTIONS
from ..externals.python import ripser, SparseRipsComplex, CechComplex, \
    WitnessComplex, StrongWitnessComplex
from ..plotting import plot_diagram
from ..utils._docs import adapt_fit_transform_docs
from ..utils.intervals import Interval
from ..utils.validation import validate_params


@adapt_fit_transform_docs
class VietorisRipsPersistence(BaseEstimator, TransformerMixin, PlotterMixin):
    """:ref:`Persistence diagrams <persistence_diagram>` resulting from
    :ref:`Vietoris–Rips filtrations
    <vietoris-rips_complex_and_vietoris-rips_persistence>`.

    Given a :ref:`point cloud <finite_metric_spaces_and_point_clouds>` in
    Euclidean space, or an abstract
    :ref:`metric space <finite_metric_spaces_and_point_clouds>` encoded by a
    distance matrix, information about the appearance and disappearance of
    topological features (technically,
    :ref:`homology classes <homology_and_cohomology>`) of various dimension
    and at different scales is summarised in the corresponding persistence
    diagram.

    Parameters
    ----------
    metric : string or callable, optional, default: ``'euclidean'``
        If set to `'precomputed'`, input data is to be interpreted as a
        collection of distance matrices. Otherwise, input data is to be
        interpreted as a collection of point clouds (i.e. feature arrays),
        and `metric` determines a rule with which to calculate distances
        between pairs of instances (i.e. rows) in these arrays.
        If `metric` is a string, it must be one of the options allowed by
        :func:`scipy.spatial.distance.pdist` for its metric parameter, or a
        metric listed in :obj:`sklearn.pairwise.PAIRWISE_DISTANCE_FUNCTIONS`,
        including "euclidean", "manhattan", or "cosine".
        If `metric` is a callable function, it is called on each pair of
        instances and the resulting value recorded. The callable should take
        two arrays from the entry in `X` as input, and return a value
        indicating the distance between them.

    homology_dimensions : list or tuple, optional, default: ``(0, 1)``
        Dimensions (non-negative integers) of the topological features to be
        detected.

    coeff : int prime, optional, default: ``2``
        Compute homology with coefficients in the prime field
        :math:`\\mathbb{F}_p = \\{ 0, \\ldots, p - 1 \\}` where
        :math:`p` equals `coeff`.

    max_edge_length : float, optional, default: ``numpy.inf``
        Upper bound on the maximum value of the Vietoris–Rips filtration
        parameter. Points whose distance is greater than this value will
        never be connected by an edge, and topological features at scales
        larger than this value will not be detected.

    infinity_values : float or None, default: ``None``
        Which death value to assign to features which are still alive at
        filtration value `max_edge_length`. ``None`` means that this
        death value is declared to be equal to `max_edge_length`.

    n_jobs : int or None, optional, default: ``None``
        The number of jobs to use for the computation. ``None`` means 1 unless
        in a :obj:`joblib.parallel_backend` context. ``-1`` means using all
        processors.

    Attributes
    ----------
    infinity_values_ : float
        Effective death value to assign to features which are still alive at
        filtration value `max_edge_length`.

    See also
    --------
    SparseRipsPersistence, EuclideanCechPersistence, CubicalPersistence,
    ConsistentRescaling

    Notes
    -----
    `Ripser <https://github.com/Ripser/ripser>`_ is used as a C++ backend
    for computing Vietoris–Rips persistent homology. Python bindings were
    modified for performance from the `ripser.py
    <https://github.com/scikit-tda/ripser.py>`_ package.

    Persistence diagrams produced by this class must be interpreted with
    care due to the presence of padding triples which carry no information.
    See :meth:`transform` for additional information.

    References
    ----------
    [1] U. Bauer, "Ripser: efficient computation of Vietoris–Rips persistence \
        barcodes", 2019; `arXiv:1908.02518 \
        <https://arxiv.org/abs/1908.02518>`_.

    """

    _hyperparameters = {
        'metric': {'type': (str, FunctionType)},
        'metric_params': {'type': dict},
        'homology_dimensions': {
            'type': (list, tuple), 'of': {
                'type': int, 'in': Interval(0, np.inf, closed='left')}},
        'coeff': {'type': int, 'in': Interval(2, np.inf, closed='left')},
        'max_edge_length': {'type': Real},
        'infinity_values': {'type': (Real, type(None))}
    }

    def __init__(self, metric='euclidean', metric_params={},
                 homology_dimensions=(0, 1), coeff=2,
                 max_edge_length=np.inf, infinity_values=None,
                 n_jobs=None):
        self.metric = metric
        self.metric_params = metric_params
        self.homology_dimensions = homology_dimensions
        self.coeff = coeff
        self.max_edge_length = max_edge_length
        self.infinity_values = infinity_values
        self.n_jobs = n_jobs

    def _ripser_diagram(self, X):
        Xdgms = ripser(X[X[:, 0] != np.inf],
                       maxdim=self._max_homology_dimension,
                       thresh=self.max_edge_length, coeff=self.coeff,
                       metric=self.metric)['dgms']

        if 0 in self._homology_dimensions:
            Xdgms[0] = Xdgms[0][:-1, :]  # Remove final death at np.inf

        # Add dimension as the third elements of each (b, d) tuple
        Xdgms = {dim: np.hstack([Xdgms[dim],
                                 dim * np.ones((Xdgms[dim].shape[0], 1),
                                               dtype=Xdgms[dim].dtype)])
                 for dim in self._homology_dimensions}
        return Xdgms

    def fit(self, X, y=None):
        """Calculate :attr:`infinity_values_`. Then, return the estimator.

        This method is here to implement the usual scikit-learn API and hence
        work in pipelines.

        Parameters
        ----------
        X : ndarray of shape (n_samples, n_points, n_points) or \
            (n_samples, n_points, n_dimensions)
            Input data. If ``metric == 'precomputed'``, the input should be an
            ndarray whose each entry along axis 0 is a distance matrix of shape
            ``(n_points, n_points)``. Otherwise, each such entry will be
            interpreted as an ndarray of ``n_points`` row vectors in
            ``n_dimensions``-dimensional space.

        y : None
            There is no need for a target in a transformer, yet the pipeline
            API requires this parameter.

        Returns
        -------
        self : object

        """
        check_array(X, allow_nd=True, force_all_finite=False)
        validate_params(
            self.get_params(), self._hyperparameters, exclude=['n_jobs'])

        if self.infinity_values is None:
            self.infinity_values_ = self.max_edge_length
        else:
            self.infinity_values_ = self.infinity_values

        self._homology_dimensions = sorted(self.homology_dimensions)
        self._max_homology_dimension = self._homology_dimensions[-1]
        return self

    def transform(self, X, y=None):
        """For each point cloud or distance matrix in `X`, compute the
        relevant persistence diagram as an array of triples [b, d, q]. Each
        triple represents a persistent topological feature in dimension q
        (belonging to `homology_dimensions`) which is born at b and dies at d.
        Only triples in which b < d are meaningful. Triples in which b and d
        are equal ("diagonal elements") may be artificially introduced during
        the computation for padding purposes, since the number of non-trivial
        persistent topological features is typically not constant across
        samples. They carry no information and hence should be effectively
        ignored by any further computation.

        Parameters
        ----------
        X : ndarray of shape (n_samples, n_points, n_points) or \
            (n_samples, n_points, n_dimensions)
            Input data. If ``metric == 'precomputed'``, the input should be an
            ndarray whose each entry along axis 0 is a distance matrix of shape
            ``(n_points, n_points)``. Otherwise, each such entry will be
            interpreted as an ndarray of ``n_points`` row vectors in
            ``n_dimensions``-dimensional space.

        y : None
            There is no need for a target in a transformer, yet the pipeline
            API requires this parameter.

        Returns
        -------
        Xt : ndarray of shape (n_samples, n_features, 3)
            Array of persistence diagrams computed from the feature arrays or
            distance matrices in `X`. ``n_features`` equals
            :math:`\\sum_q n_q`, where :math:`n_q` is the maximum number of
            topological features in dimension :math:`q` across all samples in
            `X`.

        """
        check_is_fitted(self)
        X = check_array(X, allow_nd=True, force_all_finite=False)

        Xt = Parallel(n_jobs=self.n_jobs)(delayed(self._ripser_diagram)(X[i])
                                          for i in range(len(X)))

        Xt = _postprocess_diagrams(Xt, self._homology_dimensions,
                                   self.infinity_values_, self.n_jobs)
        return Xt

    @staticmethod
    def plot(Xt, sample=0, homology_dimensions=None):
        """Plot a sample from a collection of persistence diagrams, with
        homology in multiple dimensions.

        Parameters
        ----------
        Xt : ndarray of shape (n_samples, n_points, 3)
            Collection of persistence diagrams, such as returned by
            :meth:`transform`.

        sample : int, optional, default: ``0``
            Index of the sample in `Xt` to be plotted.

        homology_dimensions : list, tuple or None, optional, default: ``None``
            Which homology dimensions to include in the plot. ``None`` means
            plotting all dimensions present in ``Xt[sample]``.

        """
        return plot_diagram(
            Xt[sample], homology_dimensions=homology_dimensions)


@adapt_fit_transform_docs
class SparseRipsPersistence(BaseEstimator, TransformerMixin, PlotterMixin):
    """:ref:`Persistence diagrams <persistence_diagram>` resulting from
    :ref:`Sparse Vietoris–Rips filtrations
    <vietoris-rips_complex_and_vietoris-rips_persistence>`.

    Given a :ref:`point cloud <finite_metric_spaces_and_point_clouds>` in
    Euclidean space, or an abstract
    :ref:`metric space <finite_metric_spaces_and_point_clouds>`
    encoded by a distance matrix, information about the appearance and
    disappearance of topological features (technically,
    :ref:`homology classes <homology_and_cohomology>`) of various dimensions
    and at different scales is summarised in the corresponding persistence
    diagram.

    Parameters
    ----------
    metric : string or callable, optional, default: ``'euclidean'``
        If set to `'precomputed'`, input data is to be interpreted as a
        collection of distance matrices. Otherwise, input data is to be
        interpreted as a collection of point clouds (i.e. feature arrays),
        and `metric` determines a rule with which to calculate distances
        between pairs of instances (i.e. rows) in these arrays.
        If `metric` is a string, it must be one of the options allowed by
        :func:`scipy.spatial.distance.pdist` for its metric parameter, or a
        metric listed in :obj:`sklearn.pairwise.PAIRWISE_DISTANCE_FUNCTIONS`,
        including "euclidean", "manhattan", or "cosine".
        If `metric` is a callable function, it is called on each pair of
        instances and the resulting value recorded. The callable should take
        two arrays from the entry in `X` as input, and return a value
        indicating the distance between them.

    metric_params : dict or None, optional, default: ``{}``
        Additional keyword arguments for the metric function.

    homology_dimensions : iterable, optional, default: ``(0, 1)``
        Dimensions (non-negative integers) of the topological features to be
        detected.

    coeff : int prime, optional, default: ``2``
        Compute homology with coefficients in the prime field
        :math:`\\mathbb{F}_p = \\{ 0, \\ldots, p - 1 \\}` where
        :math:`p` equals `coeff`.

    epsilon : float between 0. and 1., optional, default: ``0.1``
        Parameter controlling the approximation to the exact Vietoris–Rips
        filtration. If set to `0.`, :class:`SparseRipsPersistence` leads to
        the same results as :class:`VietorisRipsPersistence` but is slower.

    max_edge_length : float, optional, default: ``numpy.inf``
        Upper bound on the maximum value of the Vietoris–Rips filtration
        parameter. Points whose distance is greater than this value will
        never be connected by an edge, and topological features at scales
        larger than this value will not be detected.

    infinity_values : float or None, default : ``None``
        Which death value to assign to features which are still alive at
        filtration value `max_edge_length`. ``None`` means that this
        death value is declared to be equal to `max_edge_length`.

    n_jobs : int or None, optional, default: ``None``
        The number of jobs to use for the computation. ``None`` means 1 unless
        in a :obj:`joblib.parallel_backend` context. ``-1`` means using all
        processors.

    Attributes
    ----------
    infinity_values_ : float
        Effective death value to assign to features which are still alive at
        filtration value `max_edge_length`. Set in :meth:`fit`.

    See also
    --------
    VietorisRipsPersistence, EuclideanCechPersistence, CubicalPersistence,
    ConsistentRescaling

    Notes
    -----
    `GUDHI <https://github.com/GUDHI/gudhi-devel>`_ is used as a C++ backend
    for computing sparse Vietoris–Rips persistent homology. Python bindings
    were modified for performance.

    Persistence diagrams produced by this class must be interpreted with
    care due to the presence of padding triples which carry no information.
    See :meth:`transform` for additional information.

    References
    ----------
    [1] C. Maria, "Persistent Cohomology", 2020; `GUDHI User and Reference \
        Manual <http://gudhi.gforge.inria.fr/doc/3.1.0/group__persistent_\
        cohomology.html>`_.

    """
    _hyperparameters = {
        'metric': {'type': (str, FunctionType)},
        'metric_params': {'type': dict},
        'homology_dimensions': {
            'type': (list, tuple), 'of': {
                'type': int, 'in': Interval(0, np.inf, closed='left')}},
        'coeff': {'type': int, 'in': Interval(2, np.inf, closed='left')},
        'epsilon': {'type': Real, 'in': Interval(0, 1, closed='both')},
        'max_edge_length': {'type': Real},
        'infinity_values': {'type': (Real, type(None))}
    }

    def __init__(self, metric='euclidean', metric_params={},
                 homology_dimensions=(0, 1), coeff=2, epsilon=0.1,
                 max_edge_length=np.inf, infinity_values=None, n_jobs=None):
        self.metric = metric
        self.metric_params = metric_params
        self.homology_dimensions = homology_dimensions
        self.coeff = coeff
        self.epsilon = epsilon
        self.max_edge_length = max_edge_length
        self.infinity_values = infinity_values
        self.n_jobs = n_jobs

    def _gudhi_diagram(self, X):
        Xdgms = pairwise_distances(X, metric=self.metric, **self.metric_params)
        sparse_rips_complex = SparseRipsComplex(
            distance_matrix=Xdgms, max_edge_length=self.max_edge_length,
            sparse=self.epsilon)
        simplex_tree = sparse_rips_complex.create_simplex_tree(
            max_dimension=max(self._homology_dimensions) + 1)
        Xdgms = simplex_tree.persistence(
            homology_coeff_field=self.coeff, min_persistence=0)

        # Separate diagrams by homology dimensions
        Xdgms = {dim: np.array([Xdgms[i][1] for i in range(len(Xdgms))
                                if Xdgms[i][0] == dim]).reshape((-1, 2))
                 for dim in self.homology_dimensions}

        if 0 in self._homology_dimensions:
            Xdgms[0] = Xdgms[0][1:, :]  # Remove final death at np.inf

        # Add dimension as the third elements of each (b, d) tuple
        Xdgms = {dim: np.hstack([Xdgms[dim],
                                 dim * np.ones((Xdgms[dim].shape[0], 1),
                                               dtype=Xdgms[dim].dtype)])
                 for dim in self._homology_dimensions}
        return Xdgms

    def fit(self, X, y=None):
        """Calculate :attr:`infinity_values_`. Then, return the estimator.

        This method is here to implement the usual scikit-learn API and hence
        work in pipelines.

        Parameters
        ----------
        X : ndarray of shape (n_samples, n_points, n_points) or \
            (n_samples, n_points, n_dimensions)
            Input data. If ``metric == 'precomputed'``, the input should be an
            ndarray whose each entry along axis 0 is a distance matrix of shape
            ``(n_points, n_points)``. Otherwise, each such entry will be
            interpreted as an ndarray of ``n_points`` row vectors in
            ``n_dimensions``-dimensional space.

        y : None
            There is no need for a target in a transformer, yet the pipeline
            API requires this parameter.

        Returns
        -------
        self : object

        """
        check_array(X, allow_nd=True, force_all_finite=False)
        validate_params(
            self.get_params(), self._hyperparameters, exclude=['n_jobs'])

        if self.infinity_values is None:
            self.infinity_values_ = self.max_edge_length
        else:
            self.infinity_values_ = self.infinity_values

        self._homology_dimensions = sorted(self.homology_dimensions)
        self._max_homology_dimension = self._homology_dimensions[-1]
        return self

    def transform(self, X, y=None):
        """For each point cloud or distance matrix in `X`, compute the
        relevant persistence diagram as an array of triples [b, d, q]. Each
        triple represents a persistent topological feature in dimension q
        (belonging to `homology_dimensions`) which is born at b and dies at d.
        Only triples in which b < d are meaningful. Triples in which b and d
        are equal ("diagonal elements") may be artificially introduced during
        the computation for padding purposes, since the number of non-trivial
        persistent topological features is typically not constant across
        samples. They carry no information and hence should be effectively
        ignored by any further computation.

        Parameters
        ----------
        X : ndarray of shape (n_samples, n_points, n_points) or \
            (n_samples, n_points, n_dimensions)
            Input data. If ``metric == 'precomputed'``, the input should be an
            ndarray whose each entry along axis 0 is a distance matrix of shape
            ``(n_points, n_points)``. Otherwise, each such entry will be
            interpreted as an ndarray of ``n_points`` row vectors in
            ``n_dimensions``-dimensional space.

        y : None
            There is no need for a target in a transformer, yet the pipeline
            API requires this parameter.

        Returns
        -------
        Xt : ndarray of shape (n_samples, n_features, 3)
            Array of persistence diagrams computed from the feature arrays or
            distance matrices in `X`. ``n_features`` equals
            :math:`\\sum_q n_q`, where :math:`n_q` is the maximum number of
            topological features in dimension :math:`q` across all samples in
            `X`.

        """
        check_is_fitted(self)
        X = check_array(X, allow_nd=True, force_all_finite=False)

        Xt = Parallel(n_jobs=self.n_jobs)(
            delayed(self._gudhi_diagram)(X[i, :, :]) for i in range(
                X.shape[0]))

        Xt = _postprocess_diagrams(Xt, self._homology_dimensions,
                                   self.infinity_values_, self.n_jobs)
        return Xt

    @staticmethod
    def plot(Xt, sample=0, homology_dimensions=None):
        """Plot a sample from a collection of persistence diagrams, with
        homology in multiple dimensions.

        Parameters
        ----------
        Xt : ndarray of shape (n_samples, n_points, 3)
            Collection of persistence diagrams, such as returned by
            :meth:`transform`.

        sample : int, optional, default: ``0``
            Index of the sample in `Xt` to be plotted.

        homology_dimensions : list, tuple or None, optional, default: ``None``
            Which homology dimensions to include in the plot. ``None`` means
            plotting all dimensions present in ``Xt[sample]``.

        """
        return plot_diagram(
            Xt[sample], homology_dimensions=homology_dimensions)


@adapt_fit_transform_docs
class EuclideanCechPersistence(BaseEstimator, TransformerMixin, PlotterMixin):
    """:ref:`Persistence diagrams <persistence_diagram>` resulting from
    `Cech filtrations <TODO>`_.

    Given a :ref:`point cloud <finite_metric_spaces_and_point_clouds>` in
    Euclidean space, information about the appearance and disappearance of
    topological features (technically,
    :ref:`homology classes <homology_and_cohomology>`) of various dimensions
    and at different scales is summarised in the corresponding persistence
    diagram.

    Parameters
    ----------
    homology_dimensions : list or tuple, optional, default: ``(0, 1)``
        Dimensions (non-negative integers) of the topological features to be
        detected.

    coeff : int prime, optional, default: ``2``
        Compute homology with coefficients in the prime field
        :math:`\\mathbb{F}_p = \\{ 0, \\ldots, p - 1 \\}` where
        :math:`p` equals `coeff`.

    max_edge_length : float, optional, default: ``numpy.inf``
        Upper bound on the maximum value of the Vietoris–Rips filtration
        parameter. Points whose distance is greater than this value will
        never be connected by an edge, and topological features at scales
        larger than this value will not be detected.

    infinity_values : float or None, default: ``None``
        Which death value to assign to features which are still alive at
        filtration value `max_edge_length`. ``None`` means that this death
        value is declared to be equal to `max_edge_length`.

    n_jobs : int or None, optional, default: ``None``
        The number of jobs to use for the computation. ``None`` means 1 unless
        in a :obj:`joblib.parallel_backend` context. ``-1`` means using all
        processors.

    Attributes
    ----------
    infinity_values_ : float
        Effective death value to assign to features which are still alive at
        filtration value `max_edge_length`.

    See also
    --------
    VietorisRipsPersistence, SparseRipsPersistence, CubicalPersistence

    Notes
    -----
    `GUDHI <https://github.com/GUDHI/gudhi-devel>`_ is used as a C++ backend
    for computing Cech persistent homology. Python bindings were modified
    for performance.

    Persistence diagrams produced by this class must be interpreted with
    care due to the presence of padding triples which carry no information.
    See :meth:`transform` for additional information.

    References
    ----------
    [1] C. Maria, "Persistent Cohomology", 2020; `GUDHI User and Reference \
        Manual <http://gudhi.gforge.inria.fr/doc/3.1.0/group__persistent_\
        cohomology.html>`_.

    """

    _hyperparameters = {
        'homology_dimensions': {
            'type': (list, tuple), 'of': {
                'type': int, 'in': Interval(0, np.inf, closed='left')}},
        'coeff': {'type': int, 'in': Interval(2, np.inf, closed='left')},
        'max_edge_length': {
            'type': Real, 'in': Interval(0, np.inf, closed='right')},
        'infinity_values': {
            'type': (Real, type(None)),
            'in': Interval(0, np.inf, closed='neither')},
    }

    def __init__(self, homology_dimensions=(0, 1), coeff=2,
                 max_edge_length=np.inf, infinity_values=None, n_jobs=None):
        self.homology_dimensions = homology_dimensions
        self.coeff = coeff
        self.max_edge_length = max_edge_length
        self.infinity_values = infinity_values
        self.n_jobs = n_jobs

    def _gudhi_diagram(self, X):
        cech_complex = CechComplex(points=X, max_radius=self.max_edge_length)
        simplex_tree = cech_complex.create_simplex_tree(
            max_dimension=max(self._homology_dimensions) + 1)
        Xdgms = simplex_tree.persistence(
            homology_coeff_field=self.coeff, min_persistence=0)

        # Separate diagrams by homology dimensions
        Xdgms = {dim: np.array([Xdgms[i][1] for i in range(len(Xdgms))
                                if Xdgms[i][0] == dim]).reshape((-1, 2))
                 for dim in self.homology_dimensions}

        if 0 in self._homology_dimensions:
            Xdgms[0] = Xdgms[0][1:, :]  # Remove final death at np.inf

        # Add dimension as the third elements of each (b, d) tuple
        Xdgms = {dim: np.hstack([Xdgms[dim],
                                 dim * np.ones((Xdgms[dim].shape[0], 1),
                                               dtype=Xdgms[dim].dtype)])
                 for dim in self._homology_dimensions}
        return Xdgms

    def fit(self, X, y=None):
        """Calculate :attr:`infinity_values_`. Then, return the estimator.

        This method is here to implement the usual scikit-learn API and hence
        work in pipelines.

        Parameters
        ----------
        X : ndarray of shape (n_samples, n_points, n_dimensions)
            Input data. Each entry along axis 0 is a point cloud of
            ``n_points`` row vectors in ``n_dimensions``-dimensional space.

        y : None
            There is no need for a target in a transformer, yet the pipeline
            API requires this parameter.

        Returns
        -------
        self : object

        """
        check_array(X, allow_nd=True)
        validate_params(
            self.get_params(), self._hyperparameters, exclude=['n_jobs'])

        if self.infinity_values is None:
            self.infinity_values_ = self.max_edge_length
        else:
            self.infinity_values_ = self.infinity_values

        self._homology_dimensions = sorted(self.homology_dimensions)
        self._max_homology_dimension = self._homology_dimensions[-1]
        return self

    def transform(self, X, y=None):
        """For each point cloud in `X`, compute the relevant persistence
        diagram as an array of triples [b, d, q]. Each triple represents a
        persistent topological feature in dimension q (belonging to
        `homology_dimensions`) which is born at b and dies at d. Only triples
        in which b < d are meaningful. Triples in which b and d are equal
        ("diagonal elements") may be artificially introduced during the
        computation for padding purposes, since the number of non-trivial
        persistent topological features is typically not constant across
        samples. They carry no information and hence should be effectively
        ignored by any further computation.

        Parameters
        ----------
        X : ndarray of shape (n_samples, n_points, n_dimensions)
            Input data. Each entry along axis 0 is a point cloud of
            ``n_points`` row vectors in ``n_dimensions``-dimensional space.

        y : None
            There is no need for a target in a transformer, yet the pipeline
            API requires this parameter.

        Returns
        -------
        Xt : ndarray of shape (n_samples, n_features, 3)
            Array of persistence diagrams computed from the feature arrays in
            `X`. ``n_features`` equals :math:`\\sum_q n_q`, where :math:`n_q`
            is the maximum number of topological features in dimension
            :math:`q` across all samples in `X`.

        """
        check_is_fitted(self)
        X = check_array(X, allow_nd=True)

        Xt = Parallel(n_jobs=self.n_jobs)(
            delayed(self._gudhi_diagram)(X[i, :, :]) for i in range(
                X.shape[0]))

        Xt = _postprocess_diagrams(Xt, self._homology_dimensions,
                                   self.infinity_values_, self.n_jobs)
        return Xt


class WitnessPersistence(BaseEstimator, TransformerMixin):
    """`Persistence diagrams <https://giotto.ai/theory>`_ resulting from
    `filtrations of Witness complex <https://giotto.ai/theory>`_.

    Given a `point cloud <https://giotto.ai/theory>`_ in Euclidean space,
    information about the appearance and disappearance of
    topological features (technically, `homology classes
    <https://giotto.ai/theory>`_) of various dimensions and at different
    scales is summarised in the corresponding persistence diagram.

    Parameters
    ----------
    n_landmarks : int, optional, default: ``5``
        Number of landmarks.

    strong : bool, optional, default: ``False``
        If ``True`` computes the persistence of the strong Witness complex.
        If ``False`` computes the persistence of the weak Witness complex.

    relaxation : float, optional, default: ``0``
        Relaxation parameter for the construction of the relaxed Witness
        complex.

    subsampling: string or callable, optional, default: ``'random'``
        Rule with wich to subsample the point clouds to obtain the landmark.
        If `subsampling` is a string, it must be a subsambling function listed
        in :obj:`gtda.homology.SUBSAMPLING_FUNCTIONS`. Note that only "random"
        is currently supported.
        If `subsampling` is a callable function, it is called on each instances
        of point cloud. The callable should take a point cloud from the entry
        in `X` and a number of landmarks `n_landmarks` as inputs, and return a
        point cloud of landmarks.

    subsampling_params : dict or None, optional, default: ``{}``
        Additional keyword arguments for the subsampling function.

    metric : string or callable, optional, default: ``'euclidean'``
        Rule with which to calculate distances between pairs of instances
        (i.e. rows) in these arrays.
        If `metric` is a string, it must be one of the options allowed by
        :func:`scipy.spatial.distance.pdist` for its metric parameter, or a
        metric listed in :obj:`sklearn.pairwise.PAIRWISE_DISTANCE_FUNCTIONS`,
        including "euclidean", "manhattan" or "cosine".
        If `metric` is a callable function, it is called on each pair of
        instances and the resulting value recorded. The callable should take
        two arrays from the entry in `X` as input, and return a value
        indicating the distance between them.
        Note that metric cannot be ``'precomputed'`` as each entry in the input
        collection has to be a point clouds. Distance matrices are not
        supported.

    metric_params : dict or None, optional, default: ``{}``
        Additional keyword arguments for the metric function.

    homology_dimensions : iterable, optional, default: ``(0, 1)``
        Dimensions (non-negative integers) of the topological features to be
        detected.

    coeff : int prime, optional, default: ``2``
        Compute homology with coefficients in the prime field
        :math:`\\mathbb{F}_p = \\{ 0, \\ldots, p - 1 \\}` where
        :math:`p` equals `coeff`.

    infinity_values : float or None, default: ``None``
        Which death value to assign to features which are still alive at
        filtration value `np.inf`. ``None`` assigns the maximum pixel
        values within all images passed to meth:`fit`.

    n_jobs : int or None, optional, default: ``None``
        The number of jobs to use for the computation. ``None`` means 1 unless
        in a :obj:`joblib.parallel_backend` context. ``-1`` means using all
        processors.

    Attributes
    ----------
    infinity_values_ : float
        Effective death value to assign to features which are still alive at
        filtration value `max_edge_length`.

    See also
    --------
    VietorisRipsPersistence, SparseRipsPersistence, EuclideanCechPersistence

    Notes
    -----
    `Gudhi <https://github.com/GUDHI/gudhi-devel>`_ is used as a C++ backend
    for computing Witness persistent homology. Python bindings were modified
    for performance.

    Persistence diagrams produced by this class must be interpreted with
    care due to the presence of padding triples which carry no information.
    See :meth:`transform` for additional information.

    References
    ----------
    [1] S. Kachanovich, "Witness complex", 2015; \
        `GUDHI User and Reference Manual \
        <http://gudhi.gforge.inria.fr/doc/latest/group__witness__complex.html>`_.

    [2] V. de Silva and G. Carlsson, "Topological estimation using witness \
        complexes". Proc. Sympos. Point-Based Graphics, pages 157–166, 2004;
        `doi: 10.5555/2386332.2386359
        <https://doi.org/10.5555/2386332.2386359>`_.

    """
    _hyperparameters = {
        'metric': {'type': (str, FunctionType)},
        'metric_params': {'type': dict},
        'homology_dimensions': {
            'type': (list, tuple), 'of': {
                'type': int, 'in': Interval(0, np.inf, closed='left')}},
        'coeff': {'type': int, 'in': Interval(2, np.inf, closed='left')},
        'max_edge_length': {'type': Real},
        'infinity_values': {'type': (Real, type(None))},
        'n_landmarks':  {'type': int,
                         'in': Interval(1, np.inf, closed='left')},
        'subsampling': {'type': (str, FunctionType)},
        'subsampling_params': {'type': dict},
        'strong': {'type': bool},
        'relaxation': {'type': Real, 'in': Interval(0, np.inf, closed='left')},
    }

    def __init__(self, n_landmarks=5, strong=False, relaxation=0.,
                 subsampling='random', subsampling_params={},
                 metric='euclidean', metric_params={},
                 homology_dimensions=(0, 1), coeff=2, infinity_values=None,
                 n_jobs=None):
        self.n_landmarks = n_landmarks
        self.subsampling = subsampling
        self.subsampling_params = subsampling_params
        self.strong = strong
        self.relaxation = relaxation
        self.metric = metric
        self.metric_params = metric_params
        self.homology_dimensions = homology_dimensions
        self.coeff = coeff
        self.infinity_values = infinity_values
        self.n_jobs = n_jobs

    def _create_nearest_landmark_table(self, X):
        Xl = self._subsample(X, self.n_landmarks)
        X = pairwise_distances(X, Xl, metric=self.metric, **self.metric_params)
        L = np.argsort(X, axis=1)
        X = np.sort(X, axis=1)
        return [[(L[w][k], X[w][k]) for k in range(X.shape[1])]
                for w in range(X.shape[0])]

    def _gudhi_diagram(self, X):
        X = self._create_nearest_landmark_table(X)
        witness_complex = self._filtration(nearest_landmark_table=X)
        simplex_tree = witness_complex.create_simplex_tree(
            max_alpha_square=self.relaxation**2)
        Xdgms = simplex_tree.persistence(
            homology_coeff_field=self.coeff, min_persistence=0)

        # Separate diagrams by homology dimensions
        Xdgms = {dim: np.array([Xdgms[i][1] for i in range(len(Xdgms))
                                if Xdgms[i][0] == dim]).reshape((-1, 2))
                 for dim in self.homology_dimensions}

        # Add dimension as the third elements of each (b, d) tuple
        Xdgms = {dim: np.hstack([Xdgms[dim],
                                 dim * np.ones((Xdgms[dim].shape[0], 1),
                                               dtype=Xdgms[dim].dtype)])
                 for dim in self._homology_dimensions}
        return Xdgms

    def fit(self, X, y=None):
        """Calculate :attr:`infinity_values_`. Then, return the estimator.

        This method is there to implement the usual scikit-learn API and hence
        work in pipelines.

        Parameters
        ----------
        X : ndarray, shape (n_samples, n_pixels_1, ..., n_pixels_d)
            Input data. Array of d-dimensional images.

        y : None
            There is no need of a target in a transformer, yet the pipeline API
            requires this parameter.

        Returns
        -------
        self : object

        """

        validate_params(self.get_params(), self._hyperparameters,
                        exclude=['n_jobs'])
        check_array(X, allow_nd=True)

        if self.infinity_values is None:
            self.infinity_values_ = np.max(X)
        else:
            self.infinity_values_ = self.infinity_values

        self._homology_dimensions = sorted(self.homology_dimensions)

        self._subsample = SUBSAMPLING_FUNCTIONS[self.subsampling]

        if self.strong:
            self._filtration = StrongWitnessComplex
        else:
            self._filtration = WitnessComplex

        self._max_homology_dimension = self._homology_dimensions[-1]
        return self

    def transform(self, X, y=None):
        """For each image in `X`, compute the relevant persistence diagram
        as an array of triples [b, d, q]. Each triple represents a persistent
        topological feature in dimension q (belonging to `homology_dimensions`)
        which is born at b and dies at d. Only triples in which b < d are
        meaningful. Triples in which b and d are equal ("diagonal elements")
        may be artificially introduced during the computation for padding
        purposes, since the number of non-trivial persistent topological
        features is typically not constant across samples. They carry no
        information and hence should be effectively ignored by any further
        computation.

        Parameters
        ----------
        X : ndarray, shape (n_samples, n_pixels_1, ..., n_pixels_d)
            Input data. Array of d-dimensional images.

        y : None
            There is no need of a target in a transformer, yet the pipeline API
            requires this parameter.

        Returns
        -------
        Xt : ndarray, shape (n_samples, n_features, 3)
            Array of persistence diagrams computed from the feature arrays or
            distance matrices in `X`. ``n_features`` equals
            :math:`\\sum_q n_q`, where :math:`n_q` is the maximum number of
            topological features in dimension :math:`q` across all samples in
            `X`.

        """
        check_is_fitted(self, ['_homology_dimensions',
                               '_max_homology_dimension'])

        Xt = Parallel(n_jobs=self.n_jobs)(
            delayed(self._gudhi_diagram)(X[i, :, :]) for i in range(
                X.shape[0]))

        Xt = _postprocess_diagrams(Xt, self._homology_dimensions,
                                   self.infinity_values_, self.n_jobs)
        return Xt

    @staticmethod
    def plot(Xt, sample=0, homology_dimensions=None):
        """Plot a sample from a collection of persistence diagrams, with
        homology in multiple dimensions.

        Parameters
        ----------
        Xt : ndarray of shape (n_samples, n_points, 3)
            Collection of persistence diagrams, such as returned by
            :meth:`transform`.

        sample : int, optional, default: ``0``
            Index of the sample in `Xt` to be plotted.

        homology_dimensions : list, tuple or None, optional, default: ``None``
            Which homology dimensions to include in the plot. ``None`` means
            plotting all dimensions present in ``Xt[sample]``.

        """
        return plot_diagram(
            Xt[sample], homology_dimensions=homology_dimensions)
