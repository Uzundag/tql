#!/usr/bin/env python
import sys
import os
from time import time as timer
import traceback
import argparse

# Import modules
from tqdm import tqdm
import numpy as np
import matplotlib.pyplot as pl

from scipy.signal import detrend
import astropy.units as u
from astropy.stats import sigma_clip
from astropy.coordinates import SkyCoord
from astropy.timeseries import LombScargle
from wotan import flatten
from wotan import t14 as estimate_transit_duration
from transitleastsquares import transitleastsquares as tls
import deepdish as dd

from chronos.gls import Gls
from chronos.lightcurve import ShortCadence, LongCadence
from chronos.plot import plot_gaia_sources_on_tpf
from chronos.constants import TESS_TIME_OFFSET
from chronos.utils import (
    parse_aperture_mask,
    get_fluxes_within_mask,
    get_transit_mask,
    is_gaiaid_in_cluster,
    get_err_quadrature,
)


def plot_tql(
    gaiaid=None,
    toiid=None,
    ticid=None,
    coords=None,
    name=None,
    sector=None,
    search_radius=3,
    cadence="short",
    lctype=None,  # custom, pdcsap, sap, custom
    sap_mask=None,
    aper_radius=1,
    threshold_sigma=5,
    percentile=90,
    cutout_size=(12, 12),
    quality_bitmask="default",
    apply_data_quality_mask=False,
    flatten_method="biweight",
    window_length=0.5,  # deprecated for lk's flatten in ncadences
    Porb_limits=None,
    use_star_priors=False,
    edge_cutoff=0.1,
    sigma=(10, 3),
    run_gls=False,
    find_cluster=False,
    savefig=False,
    savetls=False,
    savegls=False,
    outdir=".",
    nearby_gaia_radius=120,  # arcsec
    bin_hr=None,
    tpf_cmap="viridis",
    verbose=True,
    clobber=False,
):
    """
    Parameters
    ----------
    cadence : str
        short, long
    lctype : str
        short=(pdcsap, sap, custom); long=(custom, cdips)
    sap_mask : str
        short=pipeline; long=square,round,threshold,percentile
    aper_radius : int
        used for square or round sap_mask (default=1 pix)
    percentile : float
        used for percentile sap_mask (default=90)
    quality_bitmask : str
        none, [default], hard, hardest; See
        https://github.com/KeplerGO/lightkurve/blob/master/lightkurve/utils.py#L135
    flatten_method : str
        wotan flatten method; See:
        https://wotan.readthedocs.io/en/latest/Interface.html#module-flatten.flatten
    window_length : float
        length in days of the filter window (default=0.5; overridden by use_star_priors)
    sigma : tuple
        sigma_lower & sigma_upper for outlier rejection after flattening
    Porb_limits : tuple
        orbital period search limits for TLS (default=None)
    use_star_priors : bool
        priors to compute t14 for detrending in wotan,
        limb darkening in tls
    edge_cutoff : float
        length in days to be cut off each edge of lightcurve (default=0.1)
    bin_hr : float
        bin size in hours of folded lightcurves
    run_gls : bool
        run Generalized Lomb Scargle (default=False)
    find_cluster : bool
        find if target is in cluster (default=False)
    Notes:
    * removes scattered light subtraction + TESSPld
    * uses wotan's biweight to flatten lightcurve
    * uses TLS to search for transit signals

    TODO:
    * rescale x-axis of phase-folded lc in days
    * add phase offset in lomb scargle plot
    """
    start = timer()
    if Porb_limits is not None:
        # assert isinstance(Porb_limits, list)
        assert len(Porb_limits) == 2, "period_min, period_max"
        Porb_min = Porb_limits[0] if Porb_limits[0] > 0.1 else None
        Porb_max = Porb_limits[1] if Porb_limits[1] > 1 else None
    else:
        Porb_min, Porb_max = None, None

    if coords is not None:
        errmsg = "coords should be a tuple (ra dec)"
        assert len(coords) == 2, errmsg
        if len(coords[0].split(":")) == 3:
            target_coord = SkyCoord(
                ra=coords[0], dec=coords[1], unit=("hourangle", "degree")
            )
        elif len(coords[0].split(".")) == 2:
            target_coord = SkyCoord(ra=coords[0], dec=coords[1], unit="degree")
        else:
            raise ValueError("cannot decode coord input")
    else:
        target_coord = None
    try:
        if cadence == "long":
            sap_mask = "square" if sap_mask is None else sap_mask
            lctype = "custom" if lctype is None else lctype
            lctypes = ["custom", "cdips", "pathos"]
            errmsg = f"{lctype} is not available in cadence=long"
            assert lctype in lctypes, errmsg
            alpha = 0.5
            lightcurve = LongCadence(
                gaiaDR2id=gaiaid,
                toiid=toiid,
                ticid=ticid,
                name=name,
                ra_deg=target_coord.ra.deg if target_coord else None,
                dec_deg=target_coord.dec.deg if target_coord else None,
                sector=sector,
                search_radius=search_radius,
                sap_mask=sap_mask,
                aper_radius=aper_radius,
                threshold_sigma=threshold_sigma,
                percentile=percentile,
                cutout_size=cutout_size,
                quality_bitmask=quality_bitmask,
                apply_data_quality_mask=apply_data_quality_mask,
                verbose=verbose,
                clobber=clobber,
            )
            bin_hr = 4 if bin_hr is None else bin_hr
            # cad = np.median(np.diff(time))
            cad = 30 / 60 / 24
        elif cadence == "short":
            sap_mask = "pipeline" if sap_mask is None else sap_mask
            lctype = "pdcsap" if lctype is None else lctype
            lctypes = ["pdcsap", "sap", "custom"]
            errmsg = f"{lctype} is not available in cadence=short"
            assert lctype in lctypes, errmsg
            alpha = 0.1
            lightcurve = ShortCadence(
                gaiaDR2id=gaiaid,
                toiid=toiid,
                ticid=ticid,
                ra_deg=target_coord.ra.deg if target_coord else None,
                dec_deg=target_coord.dec.deg if target_coord else None,
                name=name,
                sector=sector,
                search_radius=search_radius,
                sap_mask=sap_mask,
                aper_radius=aper_radius,
                threshold_sigma=threshold_sigma,
                percentile=percentile,
                quality_bitmask=quality_bitmask,
                apply_data_quality_mask=apply_data_quality_mask,
                verbose=verbose,
                clobber=clobber,
            )
            bin_hr = 0.5 if bin_hr is None else bin_hr
            cad = 2 / 60 / 24
        else:
            raise ValueError("Use cadence=(long, short).")
        if verbose:
            print(f"Analyzing {cadence} cadence data with {sap_mask} mask")
        l = lightcurve
        if l.gaia_params is None:
            _ = l.query_gaia_dr2_catalog(return_nearest_xmatch=True)
        if l.tic_params is None:
            _ = l.query_tic_catalog(return_nearest_xmatch=True)
        if not l.validate_gaia_tic_xmatch():
            raise ValueError("Gaia TIC cross-match failed")

        # +++++++++++++++++++++ raw lc
        if lctype == "custom":
            # tpf is also called to make custom lc
            lc = l.make_custom_lc()
        elif lctype == "pdcsap":
            # just downloads lightcurvefile
            lc = l.get_lc(lctype)
        elif lctype == "sap":
            # just downloads lightcurvefile;
            lc = l.get_lc(lctype)
        elif lctype == "cdips":
            errmsg = "cdips is only available for cadence=long"
            assert l.cadence == "long", errmsg
            #  just downloads fits file
            lc = l.get_cdips_lc()
            l.aper_mask = l.cdips.get_aper_mask_cdips()
        elif lctype == "pathos":
            errmsg = "pathos is only available for cadence=long"
            assert l.cadence == "long", errmsg
            #  just downloads fits file
            lc = l.get_pathos_lc()
            l.aper_mask = l.pathos.get_aper_mask_pathos()
        else:
            errmsg = "use lctype=[custom,sap,pdcsap,cdips,pathos]"
            raise ValueError(errmsg)

        if (outdir is not None) & (not os.path.exists(outdir)):
            os.makedirs(outdir)

        fig, axs = pl.subplots(3, 3, figsize=(15, 12), constrained_layout=True)
        axs = axs.flatten()

        # +++++++++++++++++++++ax: Raw + trend
        ax = axs[0]
        lc = lc.normalize().remove_nans().remove_outliers(sigma=7)
        flat, trend = lc.flatten(
            window_length=101, return_trend=True
        )  # flat and trend here are just place-holder
        time, flux = lc.time, lc.flux
        if use_star_priors:
            # for wotan and tls.power
            Rstar = (
                l.tic_params["rad"] if l.tic_params["rad"] is not None else 1.0
            )
            Mstar = (
                l.tic_params["mass"]
                if l.tic_params["mass"] is not None
                else 1.0
            )
            Porb = 10  # TODO: arbitrary default!
            tdur = estimate_transit_duration(
                R_s=Rstar, M_s=Mstar, P=Porb, small_planet=True
            )
            window_length = tdur * 3  # overrides default

        else:
            Rstar, Mstar = 1.0, 1.0

        wflat, wtrend = flatten(
            time,  # Array of time values
            flux,  # Array of flux values
            method=flatten_method,
            window_length=window_length,  # The length of the filter window in units of ``time``
            edge_cutoff=edge_cutoff,
            break_tolerance=0.1,  # Split into segments at breaks longer than that
            return_trend=True,
            cval=5.0,  # Tuning parameter for the robust estimators
        )
        # f > np.median(f) + 5 * np.std(f)
        idx = sigma_clip(
            wflat, sigma_lower=sigma[0], sigma_upper=sigma[1]
        ).mask
        # replace flux values with that from wotan
        flat = flat[~idx]
        trend = trend[~idx]
        trend.flux = wtrend[~idx]
        flat.flux = wflat[~idx]
        _ = lc.scatter(ax=ax, label="raw")
        trend.plot(ax=ax, label="trend", lw=1, c="r")

        # +++++++++++++++++++++ax2 Lomb-scargle periodogram
        ax = axs[1]
        baseline = int(time[-1] - time[0])
        Prot_max = baseline / 2

        if l.toi_params is not None:
            tmask = get_transit_mask(
                lc,
                period=l.toi_period,
                epoch=l.toi_epoch - TESS_TIME_OFFSET,
                duration_hours=l.toi_duration,
            )
            label = "masked & "
        else:
            tmask = np.zeros_like(time, dtype=bool)
            label = ""

        # detrend lc
        fraction = lc.time.shape[0] // 10
        if fraction % 2 == 0:
            fraction += 1  # add 1 if even
        dlc = lc.flatten(
            window_length=fraction, polyorder=2, break_tolerance=10, mask=tmask
        )
        # dlc = lc.copy()
        # dlc.flux = detrend(lc.flux, bp=len(lc.flux)//2)+1

        ls = LombScargle(dlc.time[~tmask], dlc.flux[~tmask])
        frequencies, powers = ls.autopower(
            minimum_frequency=1.0 / Prot_max, maximum_frequency=2.0  # 0.5 day
        )
        periods = 1.0 / frequencies
        idx = np.argmax(powers)
        best_freq = frequencies[idx]
        best_period = 1.0 / best_freq
        ax.plot(periods, powers, "k-")
        ax.axvline(
            best_period, 0, 1, ls="--", c="r", label=f"peak={best_period:.2f}"
        )
        ax.legend(title="Rotation period [d]")
        ax.set_xscale("log")
        ax.set_xlabel("Period [days]")
        ax.set_ylabel("Lomb-Scargle Power")

        if lctype == "pathos":
            # pathos do not have flux_err
            data = (dlc.time[~tmask], dlc.flux[~tmask])
        else:
            data = (dlc.time[~tmask], dlc.flux[~tmask], dlc.flux_err[~tmask])
        gls = Gls(data, Pbeg=0.1, verbose=verbose)
        if run_gls:
            if verbose:
                print("Running GLS pipeline")
            # show plot if not saved
            _ = gls.plot(block=~savefig, figsize=(10, 8))
        # +++++++++++++++++++++ax phase-folded at rotation period + sinusoidal model
        ax = axs[2]
        offset = 0.5
        t_fit = np.linspace(0, 1, 100) - offset
        y_fit = ls.model(t_fit * best_period - best_period / 2, best_freq)
        ax.plot(
            t_fit * best_period,
            y_fit,
            "r-",
            lw=3,
            label="sine model",
            zorder=3,
        )
        phase = ((time / best_period) % 1) - offset

        label += "folded at Prot"
        # plot phase-folded lc with masked transits
        a = ax.scatter(
            (phase * best_period)[~tmask],
            flux[~tmask],
            c=time[~tmask],
            label=label,
            cmap=pl.get_cmap("Blues"),
        )
        pl.colorbar(a, ax=ax, label=f"Time [BTJD]")
        ax.legend()
        ax.set_xlim(-best_period / 2, best_period / 2)
        ax.set_ylabel("Normalized Flux")
        ax.set_xlabel("Phase [days]")
        # fig.suptitle(title)

        # +++++++++++++++++++++ax5: TLS periodogram
        ax = axs[4]
        period_min = 0.1 if Porb_min is None else Porb_min
        period_max = baseline / 2 if Porb_max is None else Porb_max
        if lctype == "pathos":
            data = flat.time, flat.flux
        else:
            # err somewhat improves SDE
            data = flat.time, flat.flux, flat.flux_err
        tls_results = tls(*data).power(
            R_star=Rstar,  # 0.13-3.5 default
            R_star_max=Rstar + 0.1 if Rstar > 3.5 else 3.5,
            M_star=Mstar,  # 0.1-1
            M_star_max=Mstar + 0.1 if Mstar > 1.0 else 1.0,
            period_min=period_min,  # Roche limit default
            period_max=period_max,
            n_transits_min=2,  # default
        )

        label = f"peak={tls_results.period:.3}"
        ax.axvline(tls_results.period, alpha=0.4, lw=3, label=label)
        ax.set_xlim(np.min(tls_results.periods), np.max(tls_results.periods))

        for i in range(2, 10):
            higher_harmonics = i * tls_results.period
            if period_min <= higher_harmonics <= period_max:
                ax.axvline(
                    higher_harmonics, alpha=0.4, lw=1, linestyle="dashed"
                )
            lower_harmonics = tls_results.period / i
            if period_min <= lower_harmonics <= period_max:
                ax.axvline(
                    lower_harmonics, alpha=0.4, lw=1, linestyle="dashed"
                )
        ax.set_ylabel(r"Transit Least Squares SDE")
        ax.set_xlabel("Period (days)")
        ax.plot(tls_results.periods, tls_results.power, color="black", lw=0.5)
        ax.set_xlim(period_min, period_max)
        # do not show negative SDE
        y1, y2 = ax.get_ylim()
        y1 = 0 if y1 < 0 else y1
        ax.set_ylim(y1, y2)
        ax.legend(title="Orbital period [d]")

        # +++++++++++++++++++++++ax4 : flattened lc
        ax = axs[3]
        flat.scatter(ax=ax, label="flat", zorder=1)
        # binned phase folded lc
        nbins = int(round(bin_hr / 24 / cad))
        # transit mask
        tmask = get_transit_mask(
            flat, tls_results.period, tls_results.T0, tls_results.duration * 24
        )
        flat[tmask].scatter(ax=ax, label="transit", c="r", alpha=0.5, zorder=1)

        # +++++++++++++++++++++ax6: phase-folded at orbital period
        ax = axs[5]
        # binned phase folded lc
        fold = flat.fold(period=tls_results.period, t0=tls_results.T0)
        fold.scatter(
            ax=ax, c="k", alpha=alpha, label="folded at Porb", zorder=1
        )
        fold.bin(nbins).scatter(
            ax=ax, s=30, label=f"{bin_hr}-hr bin", zorder=2
        )

        # TLS transit model
        ax.plot(
            tls_results.model_folded_phase - offset,
            tls_results.model_folded_model,
            color="red",
            zorder=3,
            label="TLS model",
        )
        ax.set_xlabel("Phase")
        ax.set_ylabel("Relative flux")
        width = tls_results.duration / tls_results.period
        ax.set_xlim(-width * 1.5, width * 1.5)
        ax.legend()

        # +++++++++++++++++++++ax: odd-even
        ax = axs[6]
        yline = tls_results.depth
        fold.scatter(ax=ax, c="k", alpha=alpha, label="_nolegend_", zorder=1)
        fold[fold.even_mask].bin(nbins).scatter(
            label="even", s=30, ax=ax, zorder=2
        )
        ax.plot(
            tls_results.model_folded_phase - offset,
            tls_results.model_folded_model,
            color="red",
            zorder=3,
            label="TLS model",
        )
        ax.axhline(yline, 0, 1, lw=2, ls="--", c="k")
        fold[fold.odd_mask].bin(nbins).scatter(
            label="odd", s=30, ax=ax, zorder=3
        )
        ax.axhline(yline, 0, 1, lw=2, ls="--", c="k")
        ax.set_xlim(-width * 1.5, width * 1.5)
        ax.legend()

        # +++++++++++++++++++++ax7: tpf
        ax = axs[7]
        if cadence == "short":
            if l.tpf is None:
                # e.g. pdcsap, sap
                tpf = l.get_tpf()
            else:
                # e.g. custom
                tpf = l.tpf
        else:
            if l.tpf_tesscut is None:
                # e.g. cdips
                tpf = l.get_tpf_tesscut()
            else:
                # e.g. custom
                tpf = l.tpf_tesscut

        if (l.gaia_sources is None) or (nearby_gaia_radius != 120):
            _ = l.query_gaia_dr2_catalog(radius=nearby_gaia_radius)
        # _ = plot_orientation(tpf, ax)
        _ = plot_gaia_sources_on_tpf(
            tpf=tpf,
            target_gaiaid=l.gaiaid,
            gaia_sources=l.gaia_sources,
            kmax=1,
            depth=1 - tls_results.depth,
            sap_mask=l.sap_mask,
            aper_radius=l.aper_radius,
            threshold_sigma=l.threshold_sigma,
            percentile=l.percentile,
            cmap=tpf_cmap,
            dmag_limit=8,
            ax=ax,
        )

        if l.contratio is None:
            # also computed in make_custom_lc()
            l.aper_mask = parse_aperture_mask(
                tpf,
                sap_mask=l.sap_mask,
                aper_radius=l.aper_radius,
                percentile=l.percentile,
                threshold_sigma=l.threshold_sigma,
            )
            fluxes = get_fluxes_within_mask(tpf, l.aper_mask, l.gaia_sources)
            l.contratio = sum(fluxes) - 1  # c.f. l.tic_params.contratio

        # +++++++++++++++++++++ax: summary
        # add details to tls_results
        tls_results["time_raw"] = lc.time
        tls_results["flux_raw"] = lc.flux
        tls_results["time_flat"] = flat.time
        tls_results["flux_flat"] = flat.flux
        tls_results["ticid"] = l.ticid
        tls_results["sector"] = l.sector
        tls_results["cont_ratio"] = l.contratio
        # add gls_results
        tls_results["Prot_gls"] = (gls.hpstat["P"], gls.hpstat["e_P"])
        tls_results["amp_gls"] = (gls.hpstat["amp"], gls.hpstat["e_amp"])

        tp, gp = l.tic_params, l.gaia_params
        # query starhorse star params
        vizier = l.query_vizier(verbose=False)
        starhorse = (
            vizier["I/349/starhorse"]
            if "I/349/starhorse" in vizier.keys()
            else None
        )
        Mstar = (
            "nan"
            if starhorse is None
            else starhorse["mass50"].quantity[0].value
        )
        Teff = (
            "nan"
            if starhorse is None
            else starhorse["teff50"].quantity[0].value
        )
        logg = (
            "nan"
            if starhorse is None
            else starhorse["logg50"].quantity[0].value
        )
        met = (
            "nan"
            if starhorse is None
            else starhorse["met50"].quantity[0].value
        )
        if (tp["rad"] is None) or (str(tp["rad"]) == "nan"):
            # use gaia Rstar if TIC Rstar is nan
            Rstar = l.gaia_params.radius_val
            siglo = l.gaia_params.radius_percentile_lower
            sighi = l.gaia_params.radius_percentile_upper
            Rstar_err = get_err_quadrature(Rstar - siglo, sighi - Rstar)
        else:
            Rstar, Rstar_err = tp["rad"], tp["e_rad"]
        # teff = "nan" if str(tp["Teff"]).lower() == "nan" else int(tp["Teff"])
        eteff = (
            "nan" if str(tp["e_Teff"]).lower() == "nan" else int(tp["e_Teff"])
        )
        logg = tp["logg"] if logg == "nan" else logg
        met = tp["MH"] if met == "nan" else met

        ax = axs[8]
        Rp = tls_results["rp_rs"] * Rstar * u.Rsun.to(u.Rearth)
        # np.sqrt(tls_results["depth"]*(1+l.contratio))
        Rp_true = Rp * np.sqrt(1 + l.contratio)
        msg = "Candidate Properties\n"
        msg += "-" * 30 + "\n"
        # secs = ','.join(map(str, l.all_sectors))
        if l.mission == "tess":
            msg += f"SDE={tls_results.SDE:.4f} (sector={l.sector} in {l.all_sectors})\n"
        else:
            msg += f"SDE={tls_results.SDE:.4f} (campaign={l.sector} in {l.all_campaigns})\n"
        msg += (
            f"Period={tls_results.period:.4f}+/-{tls_results.period_uncertainty:.4f} d"
            + " " * 5
        )
        msg += f"T0={tls_results.T0+TESS_TIME_OFFSET:.4f} BJD\n"
        msg += f"Duration={tls_results.duration*24:.2f} hr" + " " * 10
        msg += f"Depth={(1-tls_results.depth)*100:.2f}%\n"
        msg += f"Rp={Rp:.2f} " + r"R$_{\oplus}$" + "(diluted)" + " " * 5
        msg += f"Rp={Rp_true:.2f} " + r"R$_{\oplus}$" + "(undiluted)\n"
        msg += (
            f"Odd-Even mismatch={tls_results.odd_even_mismatch:.2f}"
            + r"$\sigma$"
        )
        msg += "\n" * 2
        msg += "Stellar Properties\n"
        msg += "-" * 30 + "\n"
        msg += f"TIC ID={l.ticid}" + " " * 5
        msg += f"Tmag={tp['Tmag']:.2f}\n"
        msg += f"Gaia DR2 ID={l.gaiaid}\n"
        msg += f"Parallax={gp.parallax:.4f} mas\n"
        msg += f"GOF_AL={gp.astrometric_gof_al:.2f} (hints binarity if >20)\n"
        D = gp.astrometric_excess_noise_sig
        msg += f"astrometric excess noise sig={D:.2f} (hints binarity if >5)\n"
        msg += (
            f"Rstar={Rstar:.2f}+/-{Rstar_err:.2f} " + r"R$_{\odot}$" + " " * 5
        )
        msg += (
            f"Mstar={Mstar:.2f}+/-{tp['e_mass']:.2f} " + r"M$_{\odot}$" + "\n"
        )
        msg += f"Teff={Teff}+/-{eteff} K" + " " * 5
        msg += f"logg={logg:.2f}+/-{tp['e_logg']:.2f} cgs\n"
        msg += f"met={met:.2f}+/-{tp['e_MH']:.2f} dex\n"
        # spectype = star.get_spectral_type()
        # msg += f"SpT: {spectype}\n"
        msg += r"$\rho$" + f"star={tp['rho']:.2f}+/-{tp['e_rho']:.2f} gcc\n"
        msg += f"Contamination ratio={l.contratio:.2f}% (TIC={tp['contratio']:.2f}%)\n"
        ax.text(0, 0, msg, fontsize=10)
        ax.axis("off")

        if l.toiid is not None:
            title = f"TOI {l.toiid} | TIC {l.ticid} (sector {l.sector})"
        else:
            title = f"TIC {l.ticid} (sector {l.sector})"
        # fig.tight_layout()
        if find_cluster:
            if is_gaiaid_in_cluster(
                l.gaiaid, catalog_name="CantatGaudin2020", verbose=True
            ):
                # function prints output
                cluster_params = l.get_cluster_membership()
                # cluster_age = l.get_cluster_age(self, cluster_name=None)
                title += f" in {cluster_params.Cluster}"  # ({cluster_age})
        fig.suptitle(title)
        end = timer()
        msg = ""
        if savefig:
            fp = os.path.join(
                outdir, f"tic{l.ticid}_s{l.sector}_{lctype}_{cadence[0]}c"
            )
            fig.savefig(fp + ".png", bbox_inches="tight")
            msg += f"Saved: {fp}.png\n"
            if run_gls:
                raise NotImplementedError("To be added soon")
                # fig2.savefig(fp + "_gls.png", bbox_inches="tight")
                # msg += f"Saved: {fp}_gls.png\n"
        if savetls:
            tls_results["gaiaid"] = l.gaiaid
            tls_results["ticid"] = l.ticid
            dd.io.save(fp + "_tls.h5", tls_results)
            msg += f"Saved: {fp}_tls.h5\n"

        msg += f"#----------Runtime: {end-start:.2f} s----------#\n"
        if verbose:
            print(msg)
        return fig

    except Exception:
        # Get current system exception
        ex_type, ex_value, ex_traceback = sys.exc_info()
        # Extract unformatter stack traces as tuples
        trace_back = traceback.extract_tb(ex_traceback)

        print(f"Exception type: {ex_type.__name__}")
        print(f"Exception message: {ex_value}")
        # Format stacktrace
        for trace in trace_back:
            print(f"Line : {trace[1]}")
            print(f"Func : {trace[2]}")
            # print(f"Message : {trace[3]}")
            print(f"File : {trace[0]}")
