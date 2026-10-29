"""
Tables et colonnes sensibles, jamais lues : la validation les refuse (api_service/validate.py)
et le rôle d'exécution n'y a pas accès (tools/create_readonly_role.py). Sans dépendance.
"""

import re

FORBIDDEN_TABLES = {"ir_config_parameter", "res_users_apikeys", "auth_totp_device", "res_users_log"}
SENSITIVE_COLUMN = re.compile(r"password|secret|token|api_?key|totp|oauth|signup", re.I)
