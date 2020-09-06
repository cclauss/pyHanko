from oscrypto import keys

from . import pkcs11 as sign_pkcs11
from pkcs11 import Attribute, ObjectClass, PKCS11Error, lib as pkcs11_lib

__all__ = ['open_beid_session', 'BEIDSigner']

"""
Sign PDF files using a Belgian eID card.
"""


def open_beid_session(lib_location, slot_no=None):
    lib = pkcs11_lib(lib_location)

    slots = lib.get_slots()
    token = None
    if slot_no is None:
        for slot in slots:
            try:
                token = slot.get_token()
                if token.label == 'BELPIC':
                    break
            except PKCS11Error:
                continue
        if token is None:
            raise PKCS11Error('No BELPIC token found')
    else:
        token = slots[slot_no].get_token()
        if token.label != 'BELPIC':
            raise PKCS11Error('Token in slot %d is not BELPIC.' % slot_no)

    # the middleware will prompt for the user's PIN when we attempt
    # to sign later, so there's no need to specify it here
    return token.open()


class BEIDSigner(sign_pkcs11.PKCS11Signer):
    _issuer = None

    @property
    def issuer_cert(self):
        return self._issuer

    def _load_ca_chain(self):

        q = self.pkcs11_session.get_objects({
            Attribute.LABEL: 'CA',
            Attribute.CLASS: ObjectClass.CERTIFICATE
        })
        cert_obj, = list(q)
        intermediate_ca = keys.parse_certificate(cert_obj[Attribute.VALUE])

        q = self.pkcs11_session.get_objects({
            Attribute.LABEL: 'Root',
            Attribute.CLASS: ObjectClass.CERTIFICATE
        })
        cert_obj, = list(q)
        root_ca = keys.parse_certificate(cert_obj[Attribute.VALUE])
        self._issuer = intermediate_ca
        return [intermediate_ca, root_ca]