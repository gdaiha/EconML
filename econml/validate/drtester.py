from typing import Tuple, Union, List

import numpy as np
import pandas as pd
import scipy.stats as st
from copy import deepcopy
from sklearn.model_selection import check_cv
from sklearn.model_selection import cross_val_predict, StratifiedKFold, KFold
from statsmodels.api import WLS
from statsmodels.tools import add_constant

from econml.utilities import deprecated
from econml._cate_estimator import BaseCateEstimator

from .results import CalibrationEvaluationResults, BLPEvaluationResults, UpliftEvaluationResults, EvaluationResults
from .utils import calculate_dr_outcomes, calc_uplift
from .weighted_utils import weighted_stat, weighted_se

class DRTester:
    """
    Validation tests for CATE models.

    Includes the best linear predictor (BLP) test as in Chernozhukov et al. (2022),
    the calibration test in Dwivedi et al. (2020), and the QINI coefficient as in Radcliffe (2007).
    Has support for sample weights in all tests.

    **Best Linear Predictor (BLP)**

    Runs ordinary least squares (OLS) of doubly robust (DR) outcomes on DR outcome predictions from the CATE model
    (and a constant). If the CATE model captures true heterogeneity, then the OLS estimate on the CATE predictions
    should be positive and significantly different than 0.

    **Calibration**

    First, units are binned based on out-of-sample defined quantiles of the CATE predictions (s(Z)). Within each bin
    (k), the absolute difference between the mean CATE prediction and DR outcome is calculated, along with the
    absolute difference in the mean CATE prediction and the overall ATE. These measures are then summed across bins,
    weighted by a probability a unit is in each bin.

    .. math::

        \\mathrm{Cal}_G = \\sum_k \\pi(k) |{\\E[s(Z) | k] - \\E[Y^{DR} | k]}|

        \\mathrm{Cal}_O = \\sum_k \\pi(k) |{\\E[s(Z) | k] - \\E[Y^{DR}]}|

    The calibration r-squared is then defined as

    .. math::

        \\mathcal{R^2}_C = 1 - \\frac{\\mathrm{Cal}_G}{\\mathrm{Cal}_O}

    The calibration r-squared metric is similar to the standard R-square score in that it can take any value
    less than or equal to 1, with scores closer to 1 indicating a better calibrated CATE model.

    **Uplift Modeling**

    Units are ordered by predicted CATE values and a running measure of the average treatment effect in each cohort is
    kept as we progress through ranks. The resulting TOC curve can then be plotted and its integral calculated and used
    as a measure of true heterogeneity captured by the CATE model; this integral is referred to as the AUTOC (area
    under TOC). The QINI curve is a variant of this curve that also incorporates treatment probability; its integral is
    referred to as the QINI coefficient.

    More formally, the TOC and QINI curves are given by the following functions:

    .. math::

        \\tau_{TOC}(q) = \\mathrm{Cov}(
            Y^{DR}(g,p),
            \\frac{
                \\mathbb{1}\\{\\hat{\\tau}(Z) \\geq \\hat{\\mu}(q)\\}
            }{
                \\mathrm{Pr}(\\hat{\\tau}(Z) \\geq \\hat{\\mu}(q))
            }
        )

        \\tau_{QINI}(q) = \\mathrm{Cov}(Y^{DR}(g,p), \\mathbb{1}\\{\\hat{\\tau}(Z) \\geq \\hat{\\mu}(q)\\})

    Where :math:`q` is the desired quantile, :math:`\\hat{\\mu}` is the quantile function, and :math:`\\hat{\\tau}` is
    the predicted CATE function.
    :math:`Y^{DR}(g,p)` refers to the doubly robust outcome difference (relative to control) for the given observation.

    The AUTOC and QINI coefficient are then given by:

    .. math::

        AUTOC = \\int_0^1 \\tau_{TOC}(q) dq

        QINI = \\int_0^1 \\tau_{QINI}(q) dq

    Parameters
    ----------
    model_regression: estimator
        Nuisance model estimator used to fit the outcome to features. Must be able to implement `fit` and `predict`
        methods

    model_propensity: estimator
        Nuisance model estimator used to fit the treatment assignment to features. Must be able to implement `fit`
        method and either `predict` (in the case of binary treatment) or `predict_proba` methods (in the case of
        multiple categorical treatments).

    cate: estimator
        Fitted conditional average treatment effect (CATE) estimator to be validated.

    cv: int or list, default 5
        Splitter used for cross-validation. Can be either an integer (corresponding to the number of desired folds)
        or a list of indices corresponding to membership in each fold.

    References
    ----------
    [Chernozhukov2022] V. Chernozhukov et al.
    Generic Machine Learning Inference on Heterogeneous Treatment Effects in Randomized Experiments
    arXiv preprint arXiv:1712.04802, 2022.
    `<https://arxiv.org/abs/1712.04802>`_

    [Dwivedi2020] R. Dwivedi et al.
    Stable Discovery of Interpretable Subgroups via Calibration in Causal Studies
    arXiv preprint 	arXiv:2008.10109, 2020.
    `<https://arxiv.org/abs/2008.10109>`_

    [Radcliffe2007] N. Radcliffe
    Using control groups to target on predicted lift: Building and assessing uplift model.
    Direct Marketing Analytics Journal (2007), pages 14–21.
    """

    def __init__(
        self,
        *,
        model_regression,
        model_propensity,
        cate: BaseCateEstimator,
        cv: Union[int, List] = 5
    ):
        self.model_regression = model_regression
        self.model_propensity = model_propensity
        self.cate = cate
        self.cv = cv

    def get_cv_splitter(self, random_state: int = 123):
        """
        Generate splitter object for cross-validation.

        Checks if the cv object passed at initialization is a splitting mechanism or a number of folds and returns
        appropriately modified object for use in downstream cross-validation.

        Parameters
        ----------
        random_state: integer
            seed for splitter, default is 123

        Returns
        -------
        None, but adds attribute 'cv_splitter' containing the constructed splitter object.
        """
        splitter = check_cv(self.cv, [0], classifier=True)
        if splitter != self.cv and isinstance(splitter, (KFold, StratifiedKFold)):
            splitter.shuffle = True
            splitter.random_state = random_state
        self.cv_splitter = splitter

    def get_cv_splits(self, vars: np.ndarray, T: np.ndarray):
        """
        Generate splits for cross-validation, given a set of variables and treatment vector.

        Parameters
        ----------
        vars: (n x k) matrix or vector of length n
            Features used in nuisance models
        T: vector of length n_val
            Treatment assignment vector. Control status must be minimum value. It is recommended to have
            the control status be equal to 0, and all other treatments integers starting at 1.

        Returns
        -------
        list of folds of the data, on which to run cross-validation
        """
        if not hasattr(self, 'cv_splitter'):
            self.get_cv_splitter()

        all_vars = [var if np.ndim(var) == 2 else var.reshape(-1, 1) for var in vars if var is not None]
        to_split = np.hstack(all_vars)
        folds = self.cv_splitter.split(to_split, T)

        return list(folds)

    def fit_nuisance(
        self,
        Xval: np.ndarray,
        Dval: np.ndarray,
        yval: np.ndarray,
        Xtrain: np.ndarray = None,
        Dtrain: np.ndarray = None,
        ytrain: np.ndarray = None,
        sampleweightval: np.ndarray = None,
        sampleweighttrain: np.ndarray = None
    ):
        """
        Generate nuisance predictions and calculates doubly robust (DR) outcomes.

        Uses either (1) cross-fitting in the
        validation sample, or (2) fitting in the training sample and applying to the validation sample. If Xtrain,
        Dtrain, and ytrain are all not None, then option (2) will be implemented, otherwise, option (1) will be
        implemented. In order to use the `evaluate_cal` method then Xtrain, Dtrain, and ytrain must all be specified.

        Parameters
        ----------
        Xval: (n_val x k) matrix or vector of length n
            Features used in nuisance models for validation sample
        Dval: vector of length n_val
            Treatment assignment of validation sample. Control status must be minimum value. It is recommended to have
            the control status be equal to 0, and all other treatments integers starting at 1.
        yval: vector of length n_val
            Outcomes for the validation sample
        Xtrain: (n_train x k) matrix or vector of length n, optional
            Features used in nuisance models for training sample
        Dtrain: vector of length n_train, optional
            Treatment assignment of training sample. Control status must be minimum value. It is recommended to have
            the control status be equal to 0, and all other treatments integers starting at 1.
        ytrain: vector of length n_train, optional
            Outcomes for the training sample
        sampleweightval: vector of length n_val, optional
            Sample weights for validation sample
        sampleweighttrain: vector of length n_train, optional
            Sample weights for training sample

        Returns
        -------
        self, with added attributes for the validation treatments (Dval), treatment values (tmts),
        number of treatments excluding control (n_treat), boolean flag for whether training data is provided
        (fit_on_train), doubly robust outcome values for the validation set (dr_val), and the DR ATE value (ate_val).
        If training data is provided, also adds attributes for the doubly robust outcomes for the training
        set (dr_train) and the training treatments (Dtrain)
        """
        self.Dval = Dval

        # Unique treatments (ordered, includes control)
        self.treatments = np.sort(np.unique(Dval))

        # Number of treatments (excluding control)
        self.n_treat = len(self.treatments) - 1

        # Indicator for whether
        self.fit_on_train = (Xtrain is not None) and (Dtrain is not None) and (ytrain is not None)

        if self.fit_on_train:
            # Get DR outcomes in training sample
            reg_preds_train, prop_preds_train = self.fit_nuisance_cv(Xtrain, Dtrain, ytrain, sampleweighttrain)
            self.dr_train_ = calculate_dr_outcomes(Dtrain,
                                                   ytrain,
                                                   reg_preds_train,
                                                   prop_preds_train,
                                                   self.treatments)

            # standardize to always have 2 dimensions
            if self.dr_train_.ndim == 1:
                self.dr_train_ = self.dr_train_[..., np.newaxis]

            # Get DR outcomes in validation sample
            reg_preds_val, prop_preds_val = self.fit_nuisance_train(Xtrain,
                                                                    Dtrain,
                                                                    ytrain,
                                                                    Xval,
                                                                    sampleweighttrain)
            self.dr_val_ = calculate_dr_outcomes(Dval,
                                                 yval,
                                                 reg_preds_val,
                                                 prop_preds_val,
                                                 self.treatments)

            # standardize to always have 2 dimensions
            if self.dr_val_.ndim == 1:
                self.dr_val_ = self.dr_val_[..., np.newaxis]

        else:
            # Get DR outcomes in validation sample
            reg_preds_val, prop_preds_val = self.fit_nuisance_cv(Xval, Dval, yval, sampleweightval)
            self.dr_val_ = calculate_dr_outcomes(Dval,
                                                 yval,
                                                 reg_preds_val,
                                                 prop_preds_val,
                                                 self.treatments)

            # standardize to always have 2 dimensions
            if self.dr_val_.ndim == 1:
                self.dr_val_ = self.dr_val_[..., np.newaxis]

        # Calculate ATE in the validation sample
        self.ate_val = np.average(self.dr_val_, axis=0, weights=sampleweightval)

        return self

    def fit_nuisance_train(
        self,
        Xtrain: np.ndarray,
        Dtrain: np.ndarray,
        ytrain: np.ndarray,
        Xval: np.ndarray,
        sampleweighttrain: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Fits nuisance models in training sample and applies to generate predictions in validation sample.

        Parameters
        ----------
        Xtrain: (n_train x k) matrix
            Training sample features used to predict both treatment status and outcomes
        Dtrain: array of length n_train
            Training sample treatment assignments. Should have integer values with the lowest-value corresponding to
            the control treatment. It is recommended to have the control take value 0 and all other treatments be
            integers starting at 1
        ytrain: array of length n_train
            Outcomes for training sample
        Xval: (n_train x k) matrix
            Validation sample features used to predict both treatment status and outcomes
        sampleweighttrain: array of length n_train, optional
            Sample weights for training sample

        Returns
        -------
        2 (n_val x n_treatment + 1) arrays corresponding to the predicted outcomes under treatment status and predicted
        treatment probabilities, respectively. Both evaluated on validation set.
        """
        # set default weights if none provided
        if sampleweighttrain is None:
            sampleweighttrain = np.ones(Xtrain.shape[0])
        # Fit propensity in treatment
        self.model_propensity_ = deepcopy(self.model_propensity)
        self.model_propensity_.fit(Xtrain, Dtrain, sample_weight=sampleweighttrain)
        # Predict propensity scores
        prop_preds = self.model_propensity_.predict_proba(Xval)

        # Possible treatments (need to allow more than 2)
        n = Xval.shape[0]
        reg_preds = np.zeros((n, self.n_treat + 1))
        for i in range(self.n_treat + 1):
            model_regression_fitted = self.model_regression.fit(
                Xtrain[Dtrain == self.treatments[i]],
                ytrain[Dtrain == self.treatments[i]],
                sample_weight=sampleweighttrain[Dtrain == self.treatments[i]]
                )
            reg_preds[:, i] = model_regression_fitted.predict(Xval)

        return reg_preds, prop_preds

    def fit_nuisance_cv(
        self,
        X: np.ndarray,
        D: np.ndarray,
        y: np.ndarray,
        sampleweight: np.ndarray = None
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Generate nuisance function predictions using k-folds cross validation.

        Parameters
        ----------
        X: (n x k) matrix
            Features used to predict treatment/outcome
        D: array of length n
            Treatment assignments. Should have integer values with the lowest-value corresponding to the
            control treatment. It is recommended to have the control take value 0 and all other treatments be integers
            starting at 1
        y: array of length n
            Outcomes
        sampleweight: array of length n, optional
            Sample weights

        Returns
        -------
        2 (n x n_treatment + 1) arrays corresponding to the predicted outcomes under treatment status and predicted
        treatment probabilities, respectively.
        """
        # set default weights if none provided
        if sampleweight is None:
            sampleweight = np.ones(X.shape[0])

        splits = self.get_cv_splits([X], D)
        prop_preds = cross_val_predict(self.model_propensity, X, D, cv=splits, method='predict_proba')

        # Predict outcomes
        # T-learner logic
        N = X.shape[0]
        reg_preds = np.zeros((N, self.n_treat + 1))
        for k in range(self.n_treat + 1):
            for train, test in splits:
                model_regression_fitted = self.model_regression.fit(
                    X[train][D[train] == self.treatments[k]],
                    y[train][D[train] == self.treatments[k]],
                    sample_weight=sampleweight[train][D[train] == self.treatments[k]])
                reg_preds[test, k] = model_regression_fitted.predict(X[test])

        return reg_preds, prop_preds

    def get_cate_preds(
        self,
        Xval: np.ndarray,
        Xtrain: np.ndarray = None
    ):
        """
        Generate predictions from fitted CATE model.

        If Xtrain is None, then the predictions are generated using
        k-folds cross-validation on the validation set. If Xtrain is specified, then the CATE is assumed to have
        been fitted on the training sample (where the DR outcomes were generated using k-folds CV), and then applied
        to the validation sample.

        Parameters
        ----------
        Xval: (n_val x n_treatment) matrix
            Validation set features to be used to predict (and potentially fit) DR outcomes in CATE model
        Xtrain (n_train x n_treatment) matrix, optional
            Training set features used to fit CATE model

        Returns
        -------
        None, but adds attribute cate_preds_val_ for predicted CATE values on the validation set and, if training
        data is provided, attribute cate_preds_train_ for predicted CATE values on the training set
        """
        base = self.treatments[0]
        vals = [self.cate.effect(X=Xval, T0=base, T1=t) for t in self.treatments[1:]]

        #squeeze to avoid unnecessary dimensions with 1 value (some cases with 3 dimensions)
        self.cate_preds_val_ = np.stack(vals).T.squeeze()

        # standardize to always have 2 dimensions
        if self.cate_preds_val_.ndim == 1:
            self.cate_preds_val_ = self.cate_preds_val_[..., np.newaxis]

        if Xtrain is not None:
            trains = [self.cate.effect(X=Xtrain, T0=base, T1=t) for t in self.treatments[1:]]

            #squeeze to avoid unnecessary dimensions with 1 value (some cases with 3 dimensions)
            self.cate_preds_train_ = np.stack(trains).T.squeeze()

            # standardize to always have 2 dimensions
            if self.cate_preds_train_.ndim == 1:
                self.cate_preds_train_ = self.cate_preds_train_[..., np.newaxis]

    def evaluate_cal(
        self,
        Xval: np.ndarray = None,
        Xtrain: np.ndarray = None,
        n_groups: int = 4,
        sampleweightval: np.ndarray = None,
        sampleweighttrain: np.ndarray = None,
    ) -> CalibrationEvaluationResults:
        """
        Implement calibration test as in [Dwivedi2020].

        Parameters
        ----------
        Xval: (n_val x n_treatment) matrix, optional
            Validation sample features for CATE model. If not specified, then `fit_cate` method must already have been
            implemented
        Xtrain: (n_train x n_treatment) matrix, optional
            Training sample features for CATE model. If not specified, then `fit cate` method must already have been
            implemented (with Xtrain specified)
        n_groups: integer, default 4
            Number of quantile-based groups used to calculate calibration score.
        sampleweightval: np.ndarray = None,
            Sample weights for validation sample
        sampleweighttrain: np.ndarray = None,
            Sample weights for training sample

        Returns
        -------
        CalibrationEvaluationResults object showing the results of the calibration test
        """
        if not hasattr(self, 'dr_train_'):
            raise Exception("Must fit nuisance models on training sample data to use calibration test")

        # if CATE is given explicitly or has not been fitted at all previously, fit it now
        if (not hasattr(self, 'cate_preds_train_')) or (not hasattr(self, 'cate_preds_val_')):
            if (Xval is None) or (Xtrain is None):
                raise Exception('CATE predictions not yet calculated - must provide both Xval, Xtrain')
            self.get_cate_preds(Xval, Xtrain)

        # Default to equal weights if none provided
        if sampleweighttrain is None:
            sampleweighttrain = np.ones(self.cate_preds_train_.shape[0])
        if sampleweightval is None:
            sampleweightval = np.ones(self.cate_preds_val_.shape[0])

        # Convert weights to integer values
        sampleweightval = sampleweightval.astype(int)
        sampleweighttrain = sampleweighttrain.astype(int)

        # Check weights are valid
        assert (np.all(sampleweightval >= 1)), "Sample weights must be integer and >= 1"
        assert (np.all(sampleweighttrain >= 1)), "Sample weights must be integer and >= 1"

        cal_r_squared = np.zeros(self.n_treat)
        plot_data_dict = dict()

        for k in range(self.n_treat):

            # Determine quantile-based cuts based on training set
            cuts = weighted_stat(values=self.cate_preds_train_[:, k],
                                 q=np.linspace(0, 1, n_groups + 1),
                                 sample_weight=sampleweighttrain,
                                 mode='quantile')

            # Initialize arrays
            probs = np.zeros(n_groups)
            g_cate = np.zeros(n_groups)
            se_g_cate = np.zeros(n_groups)
            gate = np.zeros(n_groups)
            se_gate = np.zeros(n_groups)

            w_tot = np.sum(sampleweightval)

            # Calculate group statistics
            for i in range(n_groups):

                # Assign units in validation set to groups
                if i < n_groups - 1:
                    ind = (self.cate_preds_val_[:, k] >= cuts[i]) & (self.cate_preds_val_[:, k] < cuts[i + 1])
                else:
                    ind = (self.cate_preds_val_[:, k] >= cuts[i]) & (self.cate_preds_val_[:, k] <= cuts[i + 1])

                # Skip if no units in group
                if not np.any(ind):
                    continue

                w_tot_inds = np.sum(sampleweightval[ind])

                # Proportion of validations set in group
                probs[i] = w_tot_inds / w_tot

                # Group average treatment effect (GATE) -- average of DR outcomes in group
                gate[i] = np.average(self.dr_val_[ind, k], weights=sampleweightval[ind])
                se_gate[i] = weighted_se(values=self.dr_val_[ind, k], weights=sampleweightval[ind])

                # Average of CATE predictions in group
                g_cate[i] = np.average(self.cate_preds_val_[ind, k], weights=sampleweightval[ind])
                se_g_cate[i] = weighted_se(values=self.cate_preds_val_[ind, k], weights=sampleweightval[ind])

            # Calculate group calibration score
            cal_score_g = np.sum(abs(gate - g_cate) * probs)
            # Calculate overall calibration score
            cal_score_o = np.sum(abs(gate - self.ate_val[k]) * probs)
            # Calculate R-square calibration score
            cal_r_squared[k] = 1 - (cal_score_g / cal_score_o)

            df_plot = pd.DataFrame({
                'ind': np.array(range(n_groups)),
                'gate': gate,
                'se_gate': se_gate,
                'g_cate': g_cate,
                'se_g_cate': se_g_cate
            })

            plot_data_dict[self.treatments[k + 1]] = df_plot

        self.cal_res = CalibrationEvaluationResults(
            cal_r_squared=cal_r_squared,
            plot_data_dict=plot_data_dict,
            treatments=self.treatments
        )

        return self.cal_res

    def evaluate_blp(
        self,
        Xval: np.ndarray = None,
        Xtrain: np.ndarray = None,
        sampleweightval: np.ndarray = None,
    ) -> BLPEvaluationResults:
        """
        Implement the best linear predictor (BLP) test as in [Chernozhukov2022].

        `fit_nusiance` method must already have been called.

        Parameters
        ----------
        Xval: (n_val x k) matrix, optional
            Validation sample features for CATE model. If not specified, then `fit_cate` method must already have been
            implemented
        Xtrain: (n_train x k) matrix, optional
            Training sample features for CATE model. If specified, then CATE is fitted on training sample and applied
            to Xval. If specified, then Xtrain, Dtrain, Ytrain must have been specified in `fit_nuisance` method (and
            vice-versa)
        sampleweightval: np.ndarray = None,
            Sample weights for validation sample

        Returns
        -------
        BLPEvaluationResults object showing the results of the BLP test
        """
        if not hasattr(self, 'dr_val_'):
            raise Exception("Must fit nuisances before evaluating")

        # if CATE is given explicitly or has not been fitted at all previously, fit it now
        if not hasattr(self, 'cate_preds_val_'):
            if Xval is None:  # need at least Xval
                raise Exception('CATE predictions not yet calculated - must provide Xval')
            self.get_cate_preds(Xval, Xtrain)

        # Default to equal weights if none provided
        if sampleweightval is None:
            sampleweightval = np.ones(self.cate_preds_val_.shape[0])

        params = []
        errs = []
        pvals = []

        # run a separate regression for each treatment
        for k in range(self.n_treat):
            reg = WLS(self.dr_val_[:, k], add_constant(self.cate_preds_val_[:, k]),
                        weights=sampleweightval).fit(cov_type='HC1')
            params.append(reg.params[1])
            errs.append(reg.bse[1])
            pvals.append(reg.pvalues[1])

        self.blp_res = BLPEvaluationResults(
            params=params,
            errs=errs,
            pvals=pvals,
            treatments=self.treatments
        )

        return self.blp_res

    def evaluate_uplift(
        self,
        Xval: np.ndarray = None,
        Xtrain: np.ndarray = None,
        percentiles: np.ndarray = np.linspace(5, 95, 50),
        metric: str = 'qini',
        n_bootstrap: int = 1000,
        sampleweightval: np.ndarray = None,
        sampleweighttrain: np.ndarray = None
    ) -> UpliftEvaluationResults:
        """
        Calculate uplift curves and coefficients for the given model.

        Units are ordered by predicted CATE values and a running measure of the average treatment effect in each cohort
        is kept as we progress through ranks.

        The uplift coefficient is then the area under the resulting curve, with a value of 0 interpreted
        as corresponding to a model with randomly assigned CATE coefficients. All calculations are performed on
        validation dataset results, using the training set as input.

        Parameters
        ----------
        Xval: (n_val x k) matrix, optional
            Validation sample features for CATE model. If not specified, then `fit_cate` method must already have been
            implemented
        Xtrain: (n_train x k) matrix, optional
            Training sample features for CATE model. If specified, then CATE is fitted on training sample and applied
            to Xval. If specified, then Xtrain, Dtrain, Ytrain must have been specified in `fit_nuisance` method (and
            vice-versa)
        percentiles: one-dimensional array, default ``np.linspace(5, 95, 50)``
            Array of percentiles over which the QINI curve should be constructed. Defaults to 5%-95% in intervals of
            5%.
        metric: string, default 'qini'
            Which type of uplift curve to evaluate. Must be one of ['toc', 'qini']
        n_bootstrap: integer, default 1000
            Number of bootstrap samples to run when calculating uniform confidence bands.
        sampleweightval: np.ndarray = None,
            Sample weights for validation sample
        sampleweighttrain: np.ndarray = None,
            Sample weights for training sample

        Returns
        -------
        UpliftEvaluationResults object showing the fitted results
        """
        if not hasattr(self, 'dr_val_'):
            raise Exception("Must fit nuisances before evaluating")

        if (not hasattr(self, 'cate_preds_train_')) or (not hasattr(self, 'cate_preds_val_')):
            if (Xval is None) or (Xtrain is None):
                raise Exception('CATE predictions not yet calculated - must provide both Xval, Xtrain')
            self.get_cate_preds(Xval, Xtrain)

        # Default to equal weights if none provided
        if sampleweightval is None:
            sampleweightval = np.ones(self.cate_preds_val_.shape[0])
        if sampleweighttrain is None:
            sampleweighttrain = np.ones(self.cate_preds_train_.shape[0])

        # Convert weights to integer values
        sampleweightval = sampleweightval.astype(int)
        sampleweighttrain = sampleweighttrain.astype(int)

        # Check weights are valid
        assert (np.all(sampleweightval >= 1)), "Sample weights must be integer and >= 1"
        assert (np.all(sampleweighttrain >= 1)), "Sample weights must be integer and >= 1"

        curve_data_dict = dict()
        coeffs = []
        errs = []
        for k in range(self.n_treat):
            coeff, err, curve_df = calc_uplift(
                cate_preds_train=self.cate_preds_train_[:, k],
                cate_preds_val=self.cate_preds_val_[:, k],
                dr_val=self.dr_val_[:, k],
                percentiles=percentiles,
                metric=metric,
                n_bootstrap=n_bootstrap,
                sample_weight_train=sampleweighttrain,
                sample_weight_val=sampleweightval,
            )
            coeffs.append(coeff)
            errs.append(err)
            curve_data_dict[self.treatments[k + 1]] = curve_df

        pvals = [2*st.norm.sf(abs(q / e)) for q, e in zip(coeffs, errs)]

        self.uplift_res = UpliftEvaluationResults(
            params=coeffs,
            errs=errs,
            pvals=pvals,
            treatments=self.treatments,
            curve_data_dict=curve_data_dict
        )

        return self.uplift_res

    def evaluate_all(
        self,
        Xval: np.ndarray = None,
        Xtrain: np.ndarray = None,
        n_groups: int = 4,
        n_bootstrap: int = 1000,
        sampleweightval: np.ndarray = None,
        sampleweighttrain: np.ndarray = None,
    ) -> EvaluationResults:
        """
        Combine the best linear prediction, calibration, and uplift curve methods into a single summary.

        Parameters
        ----------
        Xval: (n_cal x k) matrix, optional
            Validation sample features for CATE model. If not specified, then `fit_cate` method must already have been
            implemented
        Xtrain: (n_train x k) matrix, optional
            Training sample features for CATE model. If not specified, then `fit_cate` method must already have been
            implemented
        n_groups: integer, default 4
            Number of quantile-based groups used to calculate calibration score.
        n_bootstrap: integer, default 1000
            Number of bootstrap samples to run when calculating uniform confidence bands for uplift curves.
        sampleweightval: np.ndarray = None,
            Sample weights for validation sample
        sampleweighttrain: np.ndarray = None,
            Sample weights for training sample

        Returns
        -------
        EvaluationResults object summarizing the results of all tests
        """
        if (not hasattr(self, 'cate_preds_train_')) or (not hasattr(self, 'cate_preds_val_')):
            if (Xval is None) or (Xtrain is None):
                raise Exception('CATE predictions not yet calculated - must provide both Xval, Xtrain')
            self.get_cate_preds(Xval, Xtrain)

        # Default to equal weights if none provided
        if sampleweightval is None:
            sampleweightval = np.ones(self.cate_preds_val_.shape[0])
        if sampleweighttrain is None:
            sampleweighttrain = np.ones(self.cate_preds_train_.shape[0])

        # Convert weights to integer values
        sampleweightval = sampleweightval.astype(int)
        sampleweighttrain = sampleweighttrain.astype(int)

        # Check weights are valid
        assert (np.all(sampleweightval >= 1)), "Sample weights must be integer and >= 1"
        assert (np.all(sampleweighttrain >= 1)), "Sample weights must be integer and >= 1"

        blp_res = self.evaluate_blp(Xtrain=Xtrain,
                                    Xval=Xval,
                                    sampleweightval=sampleweightval)
        cal_res = self.evaluate_cal(Xtrain=Xtrain,
                                    Xval=Xval,
                                    sampleweightval=sampleweightval,
                                    sampleweighttrain=sampleweighttrain,
                                    n_groups=n_groups)
        qini_res = self.evaluate_uplift(Xtrain=Xtrain,
                                    Xval=Xval,
                                    sampleweightval=sampleweightval,
                                    sampleweighttrain=sampleweighttrain,
                                    metric='qini',
                                    n_bootstrap=n_bootstrap)
        toc_res = self.evaluate_uplift(Xtrain=Xtrain,
                                    Xval=Xval,
                                    sampleweightval=sampleweightval,
                                    sampleweighttrain=sampleweighttrain,
                                    metric='toc',
                                    n_bootstrap=n_bootstrap)

        self.res = EvaluationResults(
            blp_res=blp_res,
            cal_res=cal_res,
            qini_res=qini_res,
            toc_res=toc_res
        )

        return self.res


@deprecated("DRtester has been renamed 'DRTester' and the old name has been deprecated and will be removed "
            "in a future release. Please use 'DRTester' instead.")
class DRtester(DRTester):
    pass
