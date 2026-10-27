"""Tests du filtrage des tables et colonnes (sans base ni modèle)."""

import os

import pytest

from schema_service.filters import (
    FilterConfig,
    TableInfo,
    decide_table,
    keep_column,
    load_filter_config,
    table_name_for_model,
)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "index_filters.yaml")


@pytest.fixture(scope="module")
def cfg() -> FilterConfig:
    return load_filter_config(CONFIG_PATH)


def decide(cfg, name, model=None, transient=False, has_meta=True):
    return decide_table(TableInfo(name=name, odoo_model=model, transient=transient), cfg, has_meta)


@pytest.mark.parametrize("name, model", [
    ("sale_order", "sale.order"),
    ("sale_order_line", "sale.order.line"),
    ("account_move", "account.move"),
    ("product_template", "product.template"),
    ("stock_move", "stock.move"),
])
def test_business_tables_are_kept(cfg, name, model):
    assert decide(cfg, name, model) == (True, None)


@pytest.mark.parametrize("name, model, reason", [
    ("ir_ui_view", "ir.ui.view", "prefix:ir_"),          # vues QWeb
    ("ir_ui_menu", "ir.ui.menu", "prefix:ir_"),          # menus
    ("ir_act_window", "ir.actions.act_window", "prefix:ir_"),
    ("mail_message", "mail.message", "prefix:mail_"),
    ("website_menu", "website.menu", "prefix:website_"),
    ("res_users_log", "res.users.log", "prefix:res_users_"),
    ("res_groups", "res.groups", "prefix:res_groups"),
    ("account_tax_sale_order_line_rel", None, "suffix:_rel"),
    ("decimal_precision", "decimal.precision", "excluded_table"),
    ("custom_table_without_model", None, "no_odoo_model"),
])
def test_technical_tables_are_excluded(cfg, name, model, reason):
    assert decide(cfg, name, model) == (False, reason)


def test_wizards_are_excluded_by_transient_flag(cfg):
    # Nom sans « wizard » : seul le drapeau transient d'Odoo permet de le détecter
    assert decide(cfg, "sale_advance_payment_inv", "sale.advance.payment.inv", transient=True) == \
        (False, "transient_model")


def test_wizard_name_patterns(cfg):
    assert decide(cfg, "account_payment_register_wizard", "x.y")[0] is False
    assert decide(cfg, "stock_wizard_line", "x.y")[0] is False


def test_whitelist_wins_over_patterns(cfg):
    # res_users est préfixé comme res_users_* mais reste indexée (liste blanche)
    assert decide(cfg, "res_users", "res.users") == (True, None)
    assert decide(cfg, "res_partner", "res.partner") == (True, None)


def test_unmapped_rule_ignored_without_odoo_metadata(cfg):
    # Base non Odoo : pas de table ir_model, on ne peut pas exclure « sans modèle »
    assert decide(cfg, "customers", None, has_meta=False) == (True, None)


@pytest.mark.parametrize("column, kept", [
    ("password", False),
    ("signup_token", False),
    ("totp_secret", False),
    ("api_key_ids", False),
    ("name", True),
    ("amount_total", True),
    ("partner_id", True),
])
def test_sensitive_columns(cfg, column, kept):
    assert keep_column(column, cfg) is kept


def test_unknown_config_key_is_rejected():
    with pytest.raises(ValueError):
        FilterConfig.from_dict({"exclude_prefix": ["ir_"]})


def test_missing_config_file_gives_defaults(tmp_path):
    cfg = load_filter_config(str(tmp_path / "absent.yaml"))
    assert cfg.exclude_transient is True
    assert cfg.exclude_prefixes == []


def test_model_to_table():
    assert table_name_for_model("sale.order.line") == "sale_order_line"
