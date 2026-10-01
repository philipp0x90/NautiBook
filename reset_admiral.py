#!/usr/bin/env python3
"""Secours de dernier recours : l'Amiral a oublié son mot de passe ET perdu son
code de secours. À lancer sur la machine qui sert NautiBook (le Pi), depuis le
dossier du projet :

    cd ~/NautiBook && .venv/bin/python reset_admiral.py

Demande un nouveau mot de passe, l'enregistre pour l'Amiral, ferme ses sessions
ouvertes, et affiche un nouveau code de secours à noter. Il faut pouvoir se
connecter au Pi (ssh) : c'est ce qui en fait un secours sûr — personne ne peut
s'en servir depuis le wifi du bord sans le mot de passe du Pi lui-même.
"""

import getpass
import sqlite3
import sys
from pathlib import Path

import passwords

DB = Path(__file__).parent / "logbook.db"


def main():
    if not DB.exists():
        sys.exit(f"Base introuvable : {DB}")
    db = sqlite3.connect(DB)
    db.execute("PRAGMA foreign_keys = ON")
    row = db.execute(
        """SELECT u.id, u.username, m.first_name, m.last_name FROM users u
           JOIN crew_members m ON u.crew_member_id = m.id WHERE u.rank = 'amiral'"""
    ).fetchone()
    if row is None:
        sys.exit("Aucun Amiral n'existe : créez-le depuis une fiche équipier (« Devenir Amiral »).")
    user_id, username, first, last = row
    print(f"Amiral : {first or ''} {(last or '').upper()} — identifiant « {username} »")
    while True:
        new = getpass.getpass("Nouveau mot de passe : ")
        if len(new) < passwords.MIN_PASSWORD_LENGTH:
            print(f"Au moins {passwords.MIN_PASSWORD_LENGTH} caractères.")
            continue
        if getpass.getpass("Encore une fois : ") != new:
            print("Les deux saisies diffèrent.")
            continue
        break
    code = passwords.new_recovery_code()
    with db:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (passwords.hash_password(new), user_id))
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        db.execute(
            "INSERT INTO app_secrets (name, value) VALUES ('admiral_recovery', ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (passwords.hash_password(passwords.normalize_recovery_code(code)),),
        )
    print("\nMot de passe de l'Amiral changé.")
    print(f"Nouveau code de secours, à noter sur papier : {code}")
    print("L'ancien code ne vaut plus.")


if __name__ == "__main__":
    main()
