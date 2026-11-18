# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
{
    "name": "Vendor Bill Capture with AI",
    "summary": "Photograph or upload a vendor bill, let an AI service read it and review the proposal",
    "description": """
Capture vendor bills from a phone or desktop (`/bill-capture`) or from the purchase journal alias.
An AI provider that can read documents proposes supplier, header data and all positions in the
draft; nothing is posted, paid or mailed. Products and the vendor prices on them are kept up to date,
low selling prices are reported. Every reading is logged.

Requires ai_translation_consensus 19.0.1.3.0 or newer (provider purposes and the ability to read
documents); the module checks this at run time.
""",
    "version": "19.0.1.0.0",
    "category": "Accounting/Accounting",
    "author": "Biricon IT Services GmbH",
    "website": "https://github.com/Alex0176/odoo-account-bill-capture",
    "license": "AGPL-3",
    "depends": ["account", "purchase", "mail", "ai_translation_consensus"],
    "data": [
        "security/ir.model.access.csv",
        "security/security.xml",
        "views/account_bill_capture_log_views.xml",
        "views/account_move_views.xml",
        "views/product_views.xml",
        "views/res_partner_views.xml",
        "views/res_config_settings_views.xml",
        "views/templates.xml",
        "wizard/account_bill_sale_price_wizard_views.xml",
        "views/menus.xml",
        "data/ir_cron_data.xml",
    ],
    "installable": True,
}
