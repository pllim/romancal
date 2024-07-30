"""
 Unit tests for the Roman source detection step code
"""

from copy import deepcopy

import numpy as np
import pytest
from astropy import units as u
from astropy.table import QTable
from photutils.datasets import make_model_image
from photutils.psf import PSFPhotometry
from roman_datamodels import maker_utils as testutil
from roman_datamodels.datamodels import ImageModel

from romancal.lib.psf import create_gridded_psf_model, fit_psf_to_image_model

n_trials = 15
image_model_shape = (50, 50)
rng = np.random.default_rng(0)


def setup_inputs(
    shape=image_model_shape,
    noise=1.0,
    seed=None,
):
    """
    Return ImageModel of level 2 image.
    """
    wfi_image = testutil.mk_level2_image(shape=shape)
    wfi_image.data = u.Quantity(
        np.ones(shape, dtype=np.float32), u.DN / u.s, dtype=np.float32
    )
    wfi_image.meta.filename = "filename"
    wfi_image.meta.instrument["optical_element"] = "F087"

    # add noise to data
    if noise is not None:
        setup_rng = np.random.default_rng(seed or 19)
        wfi_image.data = u.Quantity(
            setup_rng.normal(scale=noise, size=shape),
            u.DN / u.s,
            dtype=np.float32,
        )
        wfi_image.err = noise * np.ones(shape, dtype=np.float32) * u.DN / u.s

    # add dq array
    wfi_image.dq = np.zeros(shape, dtype=np.uint32)

    # construct ImageModel
    mod = ImageModel(wfi_image)

    filt = mod.meta.instrument["optical_element"]
    detector = mod.meta["instrument"]["detector"].replace("WFI", "SCA")

    # input parameters for PSF model:
    webbpsf_config = dict(
        filt=filt,
        detector=detector,
        oversample=12,
        fov_pixels=15,
    )

    # compute gridded PSF model:
    psf_model, centroids = create_gridded_psf_model(
        webbpsf_config["filt"],
        webbpsf_config["detector"],
        oversample=webbpsf_config["oversample"],
        fov_pixels=webbpsf_config["fov_pixels"],
        logging_level="ERROR",
    )

    return mod, webbpsf_config, psf_model


def add_sources(image_model, psf_model, x_true, y_true, flux_true, background=10):
    params_table = QTable()
    params_table["x_0"] = np.atleast_1d(x_true)
    params_table["y_0"] = np.atleast_1d(y_true)
    params_table["flux"] = np.atleast_1d(flux_true)

    shape = image_model.data.shape
    image = make_model_image(shape, psf_model, params_table, model_shape=(19, 19))
    image += rng.normal(background, 1, size=shape)

    image_model.data = image * np.ones_like(image_model.data)
    image_model.err = background * np.ones_like(image_model.err)


class TestPSFFitting:
    def setup_method(self):
        self.image_model, self.webbpsf_config, self.psf_model = setup_inputs(
            shape=image_model_shape,
        )

    @pytest.mark.webbpsf
    @pytest.mark.parametrize(
        "dx, dy, true_flux",
        zip(
            rng.uniform(-1, 1, n_trials),
            rng.uniform(-1, 1, n_trials),
            np.geomspace(1_000, 100_000, n_trials),
        ),
    )
    def test_psf_fit(self, dx, dy, true_flux):
        # generate an ImageModel
        image_model = deepcopy(self.image_model)
        init_data_stddev = np.std(image_model.data.value)

        # add synthetic sources to the ImageModel:
        true_x = image_model_shape[0] / 2 + dx
        true_y = image_model_shape[1] / 2 + dy
        add_sources(image_model, self.psf_model, true_x, true_y, true_flux)

        if self.webbpsf_config["fov_pixels"] % 2 == 0:
            fit_shape = (
                self.webbpsf_config["fov_pixels"] + 1,
                self.webbpsf_config["fov_pixels"] + 1,
            )
        else:
            fit_shape = (
                self.webbpsf_config["fov_pixels"],
                self.webbpsf_config["fov_pixels"],
            )

        # fit the PSF to the ImageModel:
        results_table, photometry = fit_psf_to_image_model(
            image_model=image_model,
            photometry_cls=PSFPhotometry,
            psf_model=self.psf_model,
            x_init=true_x,
            y_init=true_y,
            fit_shape=fit_shape,
        )

        # difference between input and output, normalized by the
        # uncertainty. Has units of sigma:
        delta_x = np.abs(true_x - results_table["x_fit"]) / results_table["x_err"]
        delta_y = np.abs(true_y - results_table["y_fit"]) / results_table["y_err"]

        sigma_threshold = 3.5
        assert np.all(delta_x < sigma_threshold)
        assert np.all(delta_y < sigma_threshold)

        # now check that the uncertainties aren't way too large, which could cause
        # the above test to pass even when the fits are bad. Use overly-simple
        # approximation that astrometric uncertainty be proportional to the
        # PSF's FWHM / SNR:
        approx_snr = true_flux / init_data_stddev
        approx_fwhm = 1
        approx_centroid_err = approx_fwhm / approx_snr

        # centroid err heuristic above is an underestimate, so we scale it up:
        scale_factor_approx = 100

        assert np.all(
            results_table["x_err"] < scale_factor_approx * approx_centroid_err
        )
        assert np.all(
            results_table["y_err"] < scale_factor_approx * approx_centroid_err
        )
