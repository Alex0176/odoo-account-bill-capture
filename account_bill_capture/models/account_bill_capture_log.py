# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import fields, models

RAW_LIMIT = 4000


class AccountBillCaptureLog(models.Model):
    """What was read by which provider, and every change of a vendor price (so it can be undone)."""

    _name = "account.bill.capture.log"
    _description = "Bill capture log"
    _order = "id desc"

    kind = fields.Selection([("read", "Reading"), ("price", "Price change"), ("create", "Product created")],
                            required=True, default="read", index=True)
    date = fields.Datetime(default=fields.Datetime.now, index=True, readonly=True)
    user_id = fields.Many2one("res.users", default=lambda self: self.env.user, readonly=True)
    company_id = fields.Many2one("res.company", default=lambda self: self.env.company, index=True)
    move_id = fields.Many2one("account.move", ondelete="set null", index=True)
    state = fields.Selection([
        ("ok", "Read"), ("invalid", "Unusable answer"), ("error", "Failed"),
        ("applied", "Applied"), ("pending", "Needs confirmation"),
        ("reverted", "Reverted"), ("dismissed", "Dismissed"),
    ], index=True)
    # reading
    origin = fields.Selection([("manual", "Button"), ("auto", "Automatic"), ("upload", "Upload"),
                               ("post", "Posting"), ("instant", "AI proposal"), ("confirm", "Confirmed")])
    provider_id = fields.Many2one("ai.translation.provider", ondelete="set null")
    provider_name = fields.Char()
    attachment_name = fields.Char()
    raw_response = fields.Text(help="The answer of the provider, shortened. Never contains a key.")
    error = fields.Char()
    problems = fields.Text()
    seconds = fields.Float()
    # price change
    product_id = fields.Many2one("product.template", ondelete="set null", index=True)
    seller_id = fields.Many2one("product.supplierinfo", ondelete="set null")
    partner_id = fields.Many2one("res.partner", ondelete="set null")
    line_id = fields.Many2one("account.move.line", ondelete="set null")
    currency_id = fields.Many2one("res.currency")
    old_price = fields.Float(digits=(16, 4))
    new_price = fields.Float(digits=(16, 4))
    created_seller = fields.Boolean(help="The vendor entry was created by this change.")
    old_values = fields.Text(help="JSON: the values of the vendor entry before the change.")
    std_changed = fields.Boolean(help="The cost of the product was changed too.")
    old_standard_price = fields.Float(digits=(16, 4))
    new_standard_price = fields.Float(digits=(16, 4))
    note = fields.Char()
