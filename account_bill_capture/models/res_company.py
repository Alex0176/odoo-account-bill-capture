# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    bill_capture_mode = fields.Selection([
        ("off", "Off"),
        ("manual", "Manual (button)"),
        ("auto", "Automatic"),
    ], string="Bill capture with AI", default="off", required=True,
        help="Off: no AI is used for vendor bills. Manual: the button 'Read with AI' on a draft bill. "
             "Automatic: new drafts with an attachment are read by a background job. Automatic only "
             "fills the draft; it never posts, pays or sends anything.")
    bill_unknown_product_mode = fields.Selection([
        ("suggest", "Only suggest"),
        ("create", "Create products automatically"),
        ("general", "Treat as general line"),
    ], string="Positions without a product", default="suggest", required=True,
        help="What happens with a position of the bill for which no existing product is found. "
             "Only suggest: line without product, hint 'Product missing' and a button to create it. "
             "Create: new product (never stocked, not for sale, not on the website); running costs "
             "become general lines. General line: text and expense account only.")
    bill_general_product = fields.Boolean(
        string="Collective product per expense account",
        help="General lines get the collective product 'General expense' of their expense account, so "
             "reports by product stay possible. The collective products are services without stock.")
    bill_supplier_price_update = fields.Selection([
        ("off", "Off"),
        ("post", "When the bill is posted"),
        ("instant", "Immediately with the AI proposal"),
    ], string="Update vendor prices", default="post", required=True,
        help="When the vendor price on the product is corrected to the price of the bill. A draft is "
             "not reviewed yet, so the default waits for the posting.")
    bill_price_change_limit = fields.Float(
        string="Price change limit (%)", default=50.0,
        help="A new price that differs from the old one by more than this is not applied; it is "
             "suggested and needs a confirmation. 0 = every change needs a confirmation.")
    bill_update_standard_price = fields.Boolean(
        string="Update cost of products without stock",
        help="Also set the cost of products that are not stocked. The cost of stocked products is "
             "never changed (it is their valuation).")
    bill_margin_check = fields.Boolean(string="Warn about low selling prices", default=True)
    bill_min_margin_percent = fields.Float(
        string="Minimum margin (%)", default=10.0,
        help="Warn when the selling price is below the purchase price, or when the margin is below "
             "this value. 0 = warn about a loss only.")
    bill_margin_activity_user_id = fields.Many2one(
        "res.users", string="Activity for low selling prices",
        help="Optional: this user gets an activity on the product. No e-mail is sent. The selling "
             "price is never changed automatically.")
