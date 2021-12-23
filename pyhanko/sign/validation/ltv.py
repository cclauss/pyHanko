import logging
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Optional, TypeVar

from asn1crypto import cms
from asn1crypto import pdf as asn1_pdf
from pyhanko_certvalidator import ValidationContext
from pyhanko_certvalidator.context import (
    CertRevTrustPolicy,
    RevocationCheckingPolicy,
    RevocationCheckingRule,
)

from pyhanko.pdf_utils.reader import PdfFileReader

from ..diff_analysis import DiffPolicy
from ..general import (
    MultivaluedAttributeError,
    NonexistentAttributeError,
    find_unique_cms_attribute,
)
from .dss import DocumentSecurityStore
from .errors import (
    NoDSSFoundError,
    SignatureValidationError,
    ValidationInfoReadingError,
)
from .generic_cms import (
    async_validate_cms_signature,
    cms_basic_validation,
    collect_signer_attr_status,
    validate_tst_signed_data,
)
from .pdf_embedded import EmbeddedPdfSignature, report_seed_value_validation
from .settings import KeyUsageConstraints
from .status import (
    PdfSignatureStatus,
    SignatureStatus,
    TimestampSignatureStatus,
)

__all__ = [
    'RevocationInfoValidationType',
    'apply_adobe_revocation_info', 'retrieve_adobe_revocation_info',
    'get_timestamp_chain', 'async_validate_pdf_ltv_signature',
]


logger = logging.getLogger(__name__)

StatusType = TypeVar('StatusType', bound=SignatureStatus)


class RevocationInfoValidationType(Enum):
    """
    Indicates a validation profile to use when validating revocation info.
    """

    ADOBE_STYLE = 'adobe'
    """
    Retrieve validation information from the CMS object, using Adobe's
    revocation info archival attribute.
    """

    PADES_LT = 'pades'
    """
    Retrieve validation information from the DSS, and require the signature's
    embedded timestamp to still be valid.
    """

    PADES_LTA = 'pades-lta'
    """
    Retrieve validation information from the DSS, but read & validate the chain
    of document timestamps leading up to the signature to establish the
    integrity of the validation information at the time of signing.
    """

    @classmethod
    def as_tuple(cls):
        return tuple(m.value for m in cls)


DEFAULT_LTV_INTERNAL_REVO_POLICY = CertRevTrustPolicy(
    RevocationCheckingPolicy(
        ee_certificate_rule=RevocationCheckingRule.CHECK_IF_DECLARED,
        intermediate_ca_cert_rule=RevocationCheckingRule.CHECK_IF_DECLARED,
    )
)

STRICT_LTV_INTERNAL_REVO_POLICY = CertRevTrustPolicy(
    RevocationCheckingPolicy(
        ee_certificate_rule=RevocationCheckingRule.CRL_OR_OCSP_REQUIRED,
        intermediate_ca_cert_rule=RevocationCheckingRule.CRL_OR_OCSP_REQUIRED,
    ),
)


def _strict_vc_context_kwargs(timestamp, validation_context_kwargs):
    # create a new validation context using the timestamp value as the time
    # of evaluation, turn off fetching and load OCSP responses / CRL data
    # from the DSS / revocation info object
    validation_context_kwargs['allow_fetching'] = False
    validation_context_kwargs['moment'] = timestamp

    # Certs with OCSP/CRL endpoints should have the relevant revocation data
    # embedded, if no stricter revocation_mode policy is in place already

    revinfo_policy: CertRevTrustPolicy \
        = validation_context_kwargs.get('revinfo_policy', None)
    if revinfo_policy is None:
        # handle legacy revocation mode
        legacy_rm = validation_context_kwargs.pop('revocation_mode', None)
        if legacy_rm and legacy_rm != 'soft-fail':
            revinfo_policy = CertRevTrustPolicy(
                RevocationCheckingPolicy.from_legacy(legacy_rm),
            )
        elif legacy_rm == 'soft-fail':
            # fall back to the default
            revinfo_policy = DEFAULT_LTV_INTERNAL_REVO_POLICY
    elif not revinfo_policy.revocation_checking_policy.essential:
        # also in this case, we sub in the default
        revinfo_policy = DEFAULT_LTV_INTERNAL_REVO_POLICY

    validation_context_kwargs['revinfo_policy'] = revinfo_policy


async def _establish_timestamp_trust(
        tst_signed_data, bootstrap_validation_context, expected_tst_imprint):
    timestamp_status_kwargs = await validate_tst_signed_data(
        tst_signed_data, bootstrap_validation_context, expected_tst_imprint
    )
    timestamp_status = TimestampSignatureStatus(**timestamp_status_kwargs)

    if not timestamp_status.valid or not timestamp_status.trusted:
        logger.warning(
            "Could not validate embedded timestamp token: %s.",
            timestamp_status.summary()
        )
        raise SignatureValidationError(
            "Could not establish time of signing, timestamp token did not "
            "validate with current settings."
        )
    return timestamp_status


def get_timestamp_chain(reader: PdfFileReader) \
        -> Iterator[EmbeddedPdfSignature]:
    """
    Get the document timestamp chain of the associated reader, ordered
    from new to old.

    :param reader:
        A :class:`.PdfFileReader`.
    :return:
        An iterable of
        :class:`~pyhanko.sign.validation.pdf_embedded.EmbeddedPdfSignature`
        objects representing document timestamps.
    """
    return filter(
        lambda sig: sig.sig_object.get('/Type', None) == '/DocTimeStamp',
        reversed(reader.embedded_signatures)
    )


@dataclass
class _TimestampTrustData:
    latest_dts: EmbeddedPdfSignature
    earliest_ts_status: TimestampSignatureStatus
    ts_chain_length: int
    current_signature_vc: ValidationContext


def _instantiate_ltv_vc(emb_timestamp: EmbeddedPdfSignature,
                        validation_context_kwargs):
    try:
        hist_resolver = emb_timestamp.reader \
            .get_historical_resolver(emb_timestamp.signed_revision)
        dss = DocumentSecurityStore.read_dss(
            hist_resolver
        )
        return dss.as_validation_context(validation_context_kwargs)
    except NoDSSFoundError:
        return ValidationContext(**validation_context_kwargs)


async def _establish_timestamp_trust_lta(
        reader, bootstrap_validation_context,
        validation_context_kwargs, until_revision):
    timestamps = get_timestamp_chain(reader)
    validation_context_kwargs = dict(validation_context_kwargs)
    current_vc = bootstrap_validation_context
    ts_status = None
    ts_count = -1
    emb_timestamp = None
    for ts_count, emb_timestamp in enumerate(timestamps):
        if emb_timestamp.signed_revision < until_revision:
            break

        emb_timestamp.compute_digest()
        ts_status = await _establish_timestamp_trust(
            emb_timestamp.signed_data, current_vc, emb_timestamp.external_digest
        )
        # set up the validation kwargs for the next iteration
        _strict_vc_context_kwargs(
            ts_status.timestamp, validation_context_kwargs
        )
        # read the DSS at the current revision into a new
        # validation context object
        current_vc = _instantiate_ltv_vc(
            emb_timestamp,
            validation_context_kwargs
        )

    return _TimestampTrustData(
        latest_dts=emb_timestamp, earliest_ts_status=ts_status,
        ts_chain_length=ts_count + 1, current_signature_vc=current_vc,
    )


# TODO verify formal PAdES requirements for timestamps
# TODO verify other formal PAdES requirements (coverage, etc.)
# TODO signature/verification policy-based validation! (PAdES-EPES-* etc)
#  (this is a different beast, though)
# TODO "tolerant" timestamp validation, where we tolerate problems in the
#  timestamp chain provided that newer timestamps are "strong" enough to
#  cover the gap.
async def async_validate_pdf_ltv_signature(
               embedded_sig: EmbeddedPdfSignature,
               validation_type: RevocationInfoValidationType,
               validation_context_kwargs=None,
               bootstrap_validation_context: Optional[ValidationContext] = None,
               ac_validation_context_kwargs=None,
               force_revinfo=False,
               diff_policy: Optional[DiffPolicy] = None,
               key_usage_settings: Optional[KeyUsageConstraints] = None,
               skip_diff: bool = False) -> PdfSignatureStatus:
    """
    .. versionadded:: 0.9.0

    Validate a PDF LTV signature according to a particular profile.

    :param embedded_sig:
        Embedded signature to evaluate.
    :param validation_type:
        Validation profile to use.
    :param validation_context_kwargs:
        Keyword args to instantiate
        :class:`.pyhanko_certvalidator.ValidationContext` objects needed over
        the course of the validation.
    :param ac_validation_context_kwargs:
        Keyword arguments for the validation context to use to
        validate attribute certificates.
        If not supplied, no AC validation will be performed.

        .. note::
            :rfc:`5755` requires attribute authority trust roots to be specified
            explicitly; hence why there's no default.
    :param bootstrap_validation_context:
        Validation context used to validate the current timestamp.
    :param force_revinfo:
        Require all certificates encountered to have some form of live
        revocation checking provisions.
    :param diff_policy:
        Policy to evaluate potential incremental updates that were appended
        to the signed revision of the document.
        Defaults to
        :const:`~pyhanko.sign.diff_analysis.DEFAULT_DIFF_POLICY`.
    :param key_usage_settings:
        A :class:`.KeyUsageConstraints` object specifying which key usages
        must or must not be present in the signer's certificate.
    :param skip_diff:
        If ``True``, skip the difference analysis step entirely.
    :return:
        The status of the signature.
    """

    # create a fresh copy of the validation_kwargs
    validation_context_kwargs: dict = dict(validation_context_kwargs or {})

    # To validate the first timestamp, allow fetching by default
    # we'll turn it off later
    validation_context_kwargs.setdefault('allow_fetching', True)
    # same for revocation_mode: if force_revinfo is false, we simply turn on
    # hard-fail by default for now. Once the timestamp is validated,
    # we switch to hard-fail forcibly.
    if force_revinfo:
        validation_context_kwargs['revinfo_policy'] \
            = STRICT_LTV_INTERNAL_REVO_POLICY
        if ac_validation_context_kwargs is not None:
            ac_validation_context_kwargs['revinfo_policy'] \
                = STRICT_LTV_INTERNAL_REVO_POLICY
    elif 'revocation_mode' not in validation_context_kwargs:
        validation_context_kwargs.setdefault(
            'revinfo_policy',
            DEFAULT_LTV_INTERNAL_REVO_POLICY
        )
        if ac_validation_context_kwargs is not None:
            ac_validation_context_kwargs.setdefault(
                'revinfo_policy',
                DEFAULT_LTV_INTERNAL_REVO_POLICY
            )

    reader = embedded_sig.reader
    if validation_type == RevocationInfoValidationType.ADOBE_STYLE:
        dss = None
        current_vc = bootstrap_validation_context or ValidationContext(
            **validation_context_kwargs
        )
    else:
        # If there's a DSS, there's no harm in reading additional certs from it
        dss = DocumentSecurityStore.read_dss(reader)
        if bootstrap_validation_context is None:
            current_vc = dss.as_validation_context(
                validation_context_kwargs, include_revinfo=False
            )
        else:
            current_vc = bootstrap_validation_context
            # add the certs from the DSS
            for cert in dss.load_certs():
                current_vc.certificate_registry.add_other_cert(cert)

    embedded_sig.compute_digest()
    embedded_sig.compute_tst_digest()

    # If the validation profile is PAdES-type, then we validate the timestamp
    #  chain now.
    #  This is bootstrapped using the current validation context.
    #  If successful, we obtain a new validation context set to a new
    #  "known good" verification time. We then repeat the process using this
    #  new validation context instead of the current one.
    # also record the embedded sig object assoc. with the oldest applicable
    # DTS in the timestamp chain
    ts_trust_data = None
    earliest_good_timestamp_st = None
    if validation_type != RevocationInfoValidationType.ADOBE_STYLE:
        ts_trust_data = await _establish_timestamp_trust_lta(
            reader, current_vc,
            validation_context_kwargs=validation_context_kwargs,
            until_revision=embedded_sig.signed_revision
        )
        current_vc = ts_trust_data.current_signature_vc
        # In PAdES-LTA, we should only rely on DSS information that is covered
        # by an appropriate document timestamp.
        # If the validation profile is PAdES-LTA, then we must have seen
        # at least one document timestamp pass by, i.e. earliest_known_timestamp
        # must be non-None by now.
        if ts_trust_data.earliest_ts_status is None \
                and validation_type == RevocationInfoValidationType.PADES_LTA:
            raise SignatureValidationError(
                "Purported PAdES-LTA signature does not have a timestamp chain."
            )
        # if this assertion fails, there's a bug in the validation code
        assert validation_type == RevocationInfoValidationType.PADES_LT \
               or ts_trust_data.ts_chain_length >= 1
        earliest_good_timestamp_st = ts_trust_data.earliest_ts_status

    # now that we have arrived at the revision with the signature,
    # we can check for a timestamp token attribute there
    # (This is allowed, regardless of whether we use Adobe-style LTV or
    # a PAdES validation profile)
    tst_signed_data = embedded_sig.attached_timestamp_data
    if tst_signed_data is not None:
        earliest_good_timestamp_st = await _establish_timestamp_trust(
            tst_signed_data, current_vc, embedded_sig.tst_signature_digest
        )
    elif validation_type == RevocationInfoValidationType.PADES_LTA \
            and ts_trust_data.ts_chain_length == 1:
        # TODO Pretty sure that this is the spirit of the LTA profile,
        #  but are we being too harsh here? I don't think so, but it's worth
        #  revisiting later
        # For later review: I believe that this check is appropriate, because
        # the timestamp that protects the signature should be verifiable
        # using only information from the next DSS, which should in turn
        # also be protected using a DTS. This requires at least two timestamps.
        raise SignatureValidationError(
            "PAdES-LTA signature requires separate timestamps protecting "
            "the signature & the rest of the revocation info."
        )

    # if, by now, we still don't have a trusted timestamp, there's a problem
    # regardless of the validation profile in use.
    if earliest_good_timestamp_st is None:
        raise SignatureValidationError(
            'LTV signatures require a trusted timestamp.'
        )

    _strict_vc_context_kwargs(
        earliest_good_timestamp_st.timestamp, validation_context_kwargs
    )

    stored_ac_vc = None
    if validation_type == RevocationInfoValidationType.ADOBE_STYLE:
        ocsps, crls = retrieve_adobe_revocation_info(
            embedded_sig.signer_info
        )
        validation_context_kwargs['ocsps'] = ocsps
        validation_context_kwargs['crls'] = crls
        stored_vc = ValidationContext(**validation_context_kwargs)
        if ac_validation_context_kwargs is not None:
            ac_validation_context_kwargs['ocsps'] = ocsps
            ac_validation_context_kwargs['crls'] = crls
            stored_ac_vc = ValidationContext(**ac_validation_context_kwargs)
    elif validation_type == RevocationInfoValidationType.PADES_LT:
        # in this case, we don't care about whether the information
        # in the DSS is protected by any timestamps, so just ingest everything
        stored_vc = dss.as_validation_context(validation_context_kwargs)
        if ac_validation_context_kwargs is not None:
            stored_ac_vc = dss.as_validation_context(
                ac_validation_context_kwargs
            )
    else:
        # in the LTA profile, we should use only DSS information covered
        # by the last relevant timestamp, so the correct VC is current_vc
        current_vc.moment = earliest_good_timestamp_st.timestamp
        stored_vc = current_vc
        if ac_validation_context_kwargs is not None:
            stored_ac_vc = _instantiate_ltv_vc(
                ts_trust_data.latest_dts, ac_validation_context_kwargs
            )
            stored_ac_vc.moment = earliest_good_timestamp_st.timestamp

    # Now, we evaluate the validity of the timestamp guaranteeing the signature
    #  *within* the LTV context.
    #   (i.e. we check whether there's enough revinfo to keep tabs on the
    #   timestamp's validity)
    # If the last timestamp comes from a timestamp token attached to the
    # signature, it should be possible to validate it using only data from the
    # DSS / revocation info store, so validate the timestamp *again*
    # using those settings.

    if tst_signed_data is not None or \
            validation_type == RevocationInfoValidationType.PADES_LT:
        if tst_signed_data is not None:
            ts_to_validate = tst_signed_data
        else:
            # we're in the PAdES-LT case with a detached TST now.
            # this should be conceptually equivalent to the above
            # so we run the same check here
            ts_to_validate = ts_trust_data.latest_dts.signed_data
        ts_status_coro = async_validate_cms_signature(
            ts_to_validate, status_cls=TimestampSignatureStatus,
            validation_context=stored_vc, status_kwargs={
                'timestamp': earliest_good_timestamp_st.timestamp
            }
        )
        timestamp_status: TimestampSignatureStatus = await ts_status_coro
    else:
        # In the LTA case, we don't have to do any further checks, since the
        # _establish_timestamp_trust_lta handled that for us.
        # We can therefore just take earliest_good_timestamp_st at face value.
        timestamp_status = earliest_good_timestamp_st

    embedded_sig.compute_integrity_info(
        diff_policy=diff_policy, skip_diff=skip_diff
    )
    status_kwargs = embedded_sig.summarise_integrity_info()
    status_kwargs.update({
        'signer_reported_dt': earliest_good_timestamp_st.timestamp,
        'timestamp_validity': timestamp_status
    })
    status_kwargs = await cms_basic_validation(
        embedded_sig.signed_data, status_cls=PdfSignatureStatus,
        raw_digest=embedded_sig.external_digest,
        validation_context=stored_vc, status_kwargs=status_kwargs,
        key_usage_settings=key_usage_settings
    )

    report_seed_value_validation(
        embedded_sig, status_kwargs['validation_path'], timestamp_found=True
    )
    if stored_ac_vc is not None:
        stored_ac_vc.certificate_registry.register_multiple(
            embedded_sig.other_embedded_certs
        )
    status_kwargs.update(
        await collect_signer_attr_status(
            sd_attr_certificates=embedded_sig.embedded_attr_certs,
            signer_cert=embedded_sig.signer_cert,
            validation_context=stored_ac_vc,
            sd_signed_attrs=embedded_sig.signer_info['signed_attrs']
        )
    )

    return PdfSignatureStatus(**status_kwargs)


def retrieve_adobe_revocation_info(signer_info: cms.SignerInfo):
    """
    Retrieve Adobe-style revocation information from a ``SignerInfo`` value,
    if present.

    Internal API.

    :param signer_info:
        A ``SignerInfo`` value.
    :return:
        A tuple of two (potentially empty) lists, containing OCSP
        responses and CRLs, respectively.
    """
    try:
        revinfo: asn1_pdf.RevocationInfoArchival = find_unique_cms_attribute(
            signer_info['signed_attrs'], "adobe_revocation_info_archival"
        )
    except (NonexistentAttributeError, MultivaluedAttributeError) as e:
        raise ValidationInfoReadingError(
            "No revocation info archival attribute found, or multiple present"
        ) from e

    ocsps = list(revinfo['ocsp'] or ())
    crls = list(revinfo['crl'] or ())
    return ocsps, crls


def apply_adobe_revocation_info(signer_info: cms.SignerInfo,
                                validation_context_kwargs=None) \
                               -> ValidationContext:
    """
    Read Adobe-style revocation information from a CMS object, and load it
    into a validation context.

    :param signer_info:
        Signer info CMS object.
    :param validation_context_kwargs:
        Extra kwargs to pass to the ``__init__`` function.
    :return:
        A validation context preloaded with the relevant revocation information.
    """
    validation_context_kwargs = validation_context_kwargs or {}
    ocsps, crls = retrieve_adobe_revocation_info(signer_info)
    return ValidationContext(
        ocsps=ocsps, crls=crls, **validation_context_kwargs
    )
