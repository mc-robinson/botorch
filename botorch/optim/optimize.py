#!/usr/bin/env python3

# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

r"""
Methods for optimizing acquisition functions.
"""

import warnings
from typing import Callable, Dict, List, Optional, Tuple, Union

import torch
from torch import Tensor
from torch.quasirandom import SobolEngine

from .. import settings
from ..acquisition import AcquisitionFunction
from ..acquisition.analytic import AnalyticAcquisitionFunction
from ..acquisition.utils import is_nonnegative
from ..exceptions import BadInitialCandidatesWarning, SamplingWarning
from ..gen import gen_candidates_scipy
from ..utils.sampling import draw_sobol_samples, manual_seed
from .initializers import initialize_q_batch, initialize_q_batch_nonneg


def optimize_acqf(
    acq_function: AcquisitionFunction,
    bounds: Tensor,
    q: int,
    num_restarts: int,
    raw_samples: int,
    options: Optional[Dict[str, Union[bool, float, int, str]]] = None,
    inequality_constraints: Optional[List[Tuple[Tensor, Tensor, float]]] = None,
    equality_constraints: Optional[List[Tuple[Tensor, Tensor, float]]] = None,
    fixed_features: Optional[Dict[int, float]] = None,
    post_processing_func: Optional[Callable[[Tensor], Tensor]] = None,
    batch_initial_conditions: Optional[Tensor] = None,
    return_best_only: bool = True,
    sequential: bool = False,
) -> Tuple[Tensor, Tensor]:
    r"""Generate a set of candidates via multi-start optimization.

    Args:
        acq_function: An AcquisitionFunction
        bounds: A `2 x d` tensor of lower and upper bounds for each column of `X`.
        q: The number of candidates.
        num_restarts:  Number of starting points for multistart acquisition
            function optimization.
        raw_samples: Number of samples for initialization
        options: Options for candidate generation.
        inequality constraints: A list of tuples (indices, coefficients, rhs),
            with each tuple encoding an inequality constraint of the form
            `\sum_i (X[indices[i]] * coefficients[i]) >= rhs`
        equality constraints: A list of tuples (indices, coefficients, rhs),
            with each tuple encoding an inequality constraint of the form
            `\sum_i (X[indices[i]] * coefficients[i]) = rhs`
        fixed_features: A map `{feature_index: value}` for features that
            should be fixed to a particular value during generation.
        post_processing_func: A function that post-processes an optimization
            result appropriately (i.e., according to `round-trip`
            transformations).
        batch_initial_conditions: A tensor to specify the initial conditions. Set
            this if you do not want to use default initialization strategy.
        return_best_only: If False, outputs the solutions corresponding to all
            random restart initializations of the optimization.
        sequential: If False, uses joint optimization, otherwise uses sequential
            optimization.

        Returns:
            A two-element tuple containing

           - a `(num_restarts) x q x d`-dim tensor of generated candidates.
           - a tensor of associated acquisiton values. If `sequential=False`,
             this is a `(num_restarts)`-dim tensor of joint acquisition values
             (with explicit restart dimension if `return_best_only=False`). If
             `sequential=True`, this is a `q`-dim tensor of expected acquisition
             values conditional on having observed canidates `0,1,...,i-1`.

        Example:
            >>> # generate `q=2` candiates jointly using 20 random restarts
            >>> # and 512 raw samples
            >>> candidates, acq_value = optimize_acqf(qEI, bounds, 2, 20, 512)

            >>> generate `q=3` candidates sequentially using 15 random restarts
            >>> # and 256 raw samples
            >>> qEI = qExpectedImprovement(model, best_f=0.2)
            >>> bounds = torch.tensor([[0.], [1.]])
            >>> candidates, acq_value_list = optimize_acqf(
            >>>     qEI, bounds, 3, 15, 256, sequential=True
            >>> )

    """
    if sequential:
        if not return_best_only:
            raise NotImplementedError(
                "return_best_only=False only supported for joint optimization"
            )
        candidate_list, acq_value_list = [], []
        candidates = torch.tensor([])
        base_X_pending = acq_function.X_pending  # pyre-ignore: [16]
        for _ in range(q):
            candidate, acq_value = optimize_acqf(
                acq_function=acq_function,
                bounds=bounds,
                q=1,
                num_restarts=num_restarts,
                raw_samples=raw_samples,
                options=options or {},
                inequality_constraints=inequality_constraints,
                equality_constraints=equality_constraints,
                fixed_features=fixed_features,
                post_processing_func=post_processing_func,
                batch_initial_conditions=None,
                return_best_only=True,
                sequential=False,
            )
            candidate_list.append(candidate)
            acq_value_list.append(acq_value)
            candidates = torch.cat(candidate_list, dim=-2)
            acq_function.set_X_pending(
                torch.cat([base_X_pending, candidates], dim=-2)
                if base_X_pending is not None
                else candidates
            )
        # Reset acq_func to previous X_pending state
        acq_function.set_X_pending(base_X_pending)
        return candidates, torch.stack(acq_value_list)

    # TODO: Generating initial candidates should use parameter constraints.
    options = options or {}
    if batch_initial_conditions is None:
        batch_initial_conditions = gen_batch_initial_conditions(
            acq_function=acq_function,
            bounds=bounds,
            q=None if isinstance(acq_function, AnalyticAcquisitionFunction) else q,
            num_restarts=num_restarts,
            raw_samples=raw_samples,
            options=options,
        )

    batch_limit: int = options.get("batch_limit", num_restarts)
    batch_candidates_list: List[Tensor] = []
    batch_acq_values_list: List[Tensor] = []
    start_idx = 0
    while start_idx < num_restarts:
        end_idx = min(start_idx + batch_limit, num_restarts)
        # optimize using random restart optimization
        batch_candidates_curr, batch_acq_values_curr = gen_candidates_scipy(
            initial_conditions=batch_initial_conditions[start_idx:end_idx],
            acquisition_function=acq_function,
            lower_bounds=bounds[0],
            upper_bounds=bounds[1],
            options={
                k: v
                for k, v in options.items()
                if k not in ("batch_limit", "nonnegative")
            },
            inequality_constraints=inequality_constraints,
            equality_constraints=equality_constraints,
            fixed_features=fixed_features,
        )
        batch_candidates_list.append(batch_candidates_curr)
        batch_acq_values_list.append(batch_acq_values_curr)
        start_idx += batch_limit
    batch_candidates = torch.cat(batch_candidates_list)
    batch_acq_values = torch.cat(batch_acq_values_list)

    if post_processing_func is not None:
        batch_candidates = post_processing_func(batch_candidates)

    if return_best_only:
        best = torch.argmax(batch_acq_values.view(-1), dim=0)
        return batch_candidates[best], batch_acq_values[best]
    else:
        return batch_candidates, batch_acq_values


def sequential_optimize(
    acq_function: AcquisitionFunction,
    bounds: Tensor,
    q: int,
    num_restarts: int,
    raw_samples: int,
    options: Optional[Dict[str, Union[bool, float, int]]] = None,
    inequality_constraints: Optional[List[Tuple[Tensor, Tensor, float]]] = None,
    equality_constraints: Optional[List[Tuple[Tensor, Tensor, float]]] = None,
    fixed_features: Optional[Dict[int, float]] = None,
    post_processing_func: Optional[Callable[[Tensor], Tensor]] = None,
) -> Tuple[Tensor, Tensor]:
    r"""DEPRECATED - Use optimize_acqf with sequential=True instead."""
    warnings.warn(
        "sequential_optimize is deprecated. Please use optimize_acfq "
        "with sequential=True instead.",
        DeprecationWarning,
    )
    candidates, acq_values = optimize_acqf(
        acq_function=acq_function,
        bounds=bounds,
        q=q,
        num_restarts=num_restarts,
        raw_samples=raw_samples,
        options=options,
        inequality_constraints=inequality_constraints,
        equality_constraints=equality_constraints,
        fixed_features=fixed_features,
        post_processing_func=post_processing_func,
        batch_initial_conditions=None,
        return_best_only=True,
        sequential=True,
    )

    return candidates, acq_values


def joint_optimize(
    acq_function: AcquisitionFunction,
    bounds: Tensor,
    q: int,
    num_restarts: int,
    raw_samples: int,
    options: Optional[Dict[str, Union[bool, float, int]]] = None,
    inequality_constraints: Optional[List[Tuple[Tensor, Tensor, float]]] = None,
    equality_constraints: Optional[List[Tuple[Tensor, Tensor, float]]] = None,
    fixed_features: Optional[Dict[int, float]] = None,
    post_processing_func: Optional[Callable[[Tensor], Tensor]] = None,
    batch_initial_conditions: Optional[Tensor] = None,
    return_best_only: bool = True,
) -> Tuple[Tensor, Tensor]:
    r"""DEPRECATED - Use optimize_acqf instead."""
    warnings.warn(
        "joint_optimize is deprecated. Please use optimize_acfq instead.",
        DeprecationWarning,
    )

    batch_candidates, batch_acq_values = optimize_acqf(
        acq_function=acq_function,
        bounds=bounds,
        q=q,
        num_restarts=num_restarts,
        raw_samples=raw_samples,
        options=options,
        inequality_constraints=inequality_constraints,
        equality_constraints=equality_constraints,
        fixed_features=fixed_features,
        post_processing_func=post_processing_func,
        batch_initial_conditions=batch_initial_conditions,
        return_best_only=return_best_only,
        sequential=False,
    )

    return batch_candidates, batch_acq_values


def gen_batch_initial_conditions(
    acq_function: AcquisitionFunction,
    bounds: Tensor,
    q: int,
    num_restarts: int,
    raw_samples: int,
    options: Optional[Dict[str, Union[bool, float, int]]] = None,
) -> Tensor:
    r"""Generate a batch of initial conditions for random-restart optimziation.

    Args:
        acq_function: The acquisition function to be optimized.
        bounds: A `2 x d` tensor of lower and upper bounds for each column of `X`.
        q: The number of candidates to consider.
        num_restarts: The number of starting points for multistart acquisition
            function optimization.
        raw_samples: The number of raw samples to consider in the initialization
            heuristic.
        options: Options for initial condition generation. For valid options see
            `initialize_q_batch` and `initialize_q_batch_nonneg`. If `options`
            contains a `nonnegative=True` entry, then `acq_function` is
            assumed to be non-negative (useful when using custom acquisition
            functions).

    Returns:
        A `num_restarts x q x d` tensor of initial conditions.

    Example:
        >>> qEI = qExpectedImprovement(model, best_f=0.2)
        >>> bounds = torch.tensor([[0.], [1.]])
        >>> Xinit = gen_batch_initial_conditions(
        >>>     qEI, bounds, q=3, num_restarts=25, raw_samples=500
        >>> )

    """
    options = options or {}
    seed: Optional[int] = options.get("seed")
    batch_limit: Optional[int] = options.get("batch_limit")
    batch_initial_arms: Tensor
    factor, max_factor = 1, 5
    init_kwargs = {}
    if "eta" in options:
        init_kwargs["eta"] = options.get("eta")
    if options.get("nonnegative") or is_nonnegative(acq_function):
        init_func = initialize_q_batch_nonneg
        if "alpha" in options:
            init_kwargs["alpha"] = options.get("alpha")
    else:
        init_func = initialize_q_batch

    q = 1 if q is None else q
    # the dimension the samples are drawn from
    dim = bounds.shape[-1] * q
    if dim > SobolEngine.MAXDIM and settings.debug.on():
        warnings.warn(
            f"Sample dimension q*d={dim} exceeding Sobol max dimension "
            f"({SobolEngine.MAXDIM}). Using iid samples instead.",
            SamplingWarning,
        )

    while factor < max_factor:
        with warnings.catch_warnings(record=True) as ws:
            n = raw_samples * factor
            if dim <= SobolEngine.MAXDIM:
                X_rnd = draw_sobol_samples(bounds=bounds, n=n, q=q, seed=seed)
            else:
                with manual_seed(seed):
                    X_rnd_nlzd = torch.rand(
                        n * dim, device=bounds.device, dtype=bounds.dtype
                    ).view(n, q, bounds.shape[-1])
                X_rnd = bounds[0] + (bounds[1] - bounds[0]) * X_rnd_nlzd
            with torch.no_grad():
                if batch_limit is None:
                    batch_limit = X_rnd.shape[0]
                Y_rnd_list = []
                start_idx = 0
                while start_idx < X_rnd.shape[0]:
                    end_idx = min(start_idx + batch_limit, X_rnd.shape[0])
                    Y_rnd_curr = acq_function(X_rnd[start_idx:end_idx])
                    Y_rnd_list.append(Y_rnd_curr)
                    start_idx += batch_limit
                Y_rnd = torch.cat(Y_rnd_list).to(X_rnd)
            batch_initial_conditions = init_func(
                X=X_rnd, Y=Y_rnd, n=num_restarts, **init_kwargs
            )
            if not any(issubclass(w.category, BadInitialCandidatesWarning) for w in ws):
                return batch_initial_conditions
            if factor < max_factor:
                factor += 1
                if seed is not None:
                    seed += 1  # make sure to sample different X_rnd
    warnings.warn(
        "Unable to find non-zero acquisition function values - initial conditions "
        "are being selected randomly.",
        BadInitialCandidatesWarning,
    )
    return batch_initial_conditions
