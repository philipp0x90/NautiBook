"""Mots de passe et codes de secours : hachage, vérification, génération.

Module à part, sans dépendance à l'app, parce que reset_admiral.py s'en sert
aussi, et qu'importer main.py depuis un script lancerait toute l'application.

Le hachage se fait **côté serveur**, avec scrypt (bibliothèque standard, rien à
installer sur le Pi) : une fonction volontairement lente et gourmande en
mémoire, avec un sel aléatoire par mot de passe, pour qu'une base volée ne se
laisse pas inverser par force brute. Le transport, lui, reste en HTTP sur le
wifi du bord, risque accepté (voir CLAUDE.md).
"""

import hashlib
import hmac
import secrets

# Coût de scrypt : n = 2**14, r = 8 demande 16 Mo de mémoire et quelques
# dizaines de millisecondes par essai sur un Raspberry Pi — imperceptible à la
# connexion, ruineux pour qui voudrait en essayer des millions.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD_LENGTH = 8

# Alphabet des codes de secours : sans 0/O, 1/I/L, pour qu'un code recopié à la
# main sur papier se relise sans erreur.
RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def hash_password(password: str) -> str:
    """Mot de passe → « scrypt$n$r$p$sel$hachage », tout en hexadécimal.
    Les paramètres sont rangés avec le hachage, pour qu'un réglage plus coûteux
    plus tard n'invalide pas les mots de passe déjà enregistrés."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    """Le mot de passe correspond-il au hachage enregistré ? Comparaison à temps
    constant (hmac.compare_digest), pour ne rien laisser deviner par la durée."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                                   n=int(n), r=int(r), p=int(p), dklen=len(digest) // 2)
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


# Un hachage valide pour un mot de passe que personne ne connaît : la connexion
# le vérifie quand l'identifiant n'existe pas, pour que la réponse prenne le
# même temps et ne révèle pas quels identifiants existent.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def new_recovery_code() -> str:
    """Code de secours de l'Amiral, en quatre groupes : « K7PQ-3MXT-9WRA-H2CD »."""
    chars = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(16))
    return "-".join(chars[i:i + 4] for i in range(0, 16, 4))


def normalize_recovery_code(code: str) -> str:
    """Comme recopié par quelqu'un : minuscules, espaces et tirets oubliés ou
    en trop ne doivent pas faire échouer un code juste."""
    return "".join(c for c in (code or "").upper() if c.isalnum())
