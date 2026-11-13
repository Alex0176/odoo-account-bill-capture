# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    bill_capture_mode = fields.Selection(related="company_id.bill_capture_mode", readonly=False)
    bill_unknown_product_mode = fields.Selection(related="company_id.bill_unknown_product_mode", readonly=False)
    bill_general_product = fields.Boolean(related="company_id.bill_general_product", readonly=False)
    bill_supplier_price_update = fields.Selection(related="company_id.bill_supplier_price_update", readonly=False)
    bill_price_change_limit = fields.Float(related="company_id.bill_price_change_limit", readonly=False)
    bill_update_standard_price = fields.Boolean(related="company_id.bill_update_standard_price", readonly=False)
    bill_margin_check = fields.Boolean(related="company_id.bill_margin_check", readonly=False)
    bill_min_margin_percent = fields.Float(related="company_id.bill_min_margin_percent", readonly=False)
    bill_margin_activity_user_id = fields.Many2one(related="company_id.bill_margin_activity_user_id", readonly=False)
