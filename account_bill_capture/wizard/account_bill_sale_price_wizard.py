# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class AccountBillSalePriceWizard(models.TransientModel):
    """Proposal for a new selling price after a purchase price went up. Nothing changes without a click."""

    _name = "account.bill.sale.price.wizard"
    _description = "Adjust selling price"

    line_id = fields.Many2one("account.move.line", required=True, readonly=True)
    product_tmpl_id = fields.Many2one("product.template", required=True, readonly=True, string="Product")
    company_currency_id = fields.Many2one("res.currency", required=True, readonly=True)
    cost = fields.Monetary(currency_field="company_currency_id", readonly=True,
                           help="Purchase price per selling unit, company currency.")
    current_price = fields.Monetary(currency_field="company_currency_id", readonly=True)
    markup_percent = fields.Float(string="Markup on cost (%)")
    new_price = fields.Monetary(currency_field="company_currency_id")

    @api.model
    def _open_for(self, line):
        line.ensure_one()
        move = line.move_id
        move._bill_ai_check_rights()
        if not line.product_id:
            raise UserError(_("The line has no product."))
        tmpl = line.product_id.product_tmpl_id
        cost = move._bill_cost_per_product_unit(line)
        # a markup that reaches the minimum margin; 30 % when none is set
        minimum = move.company_id.bill_min_margin_percent or 0.0
        markup = round(100.0 * minimum / (100.0 - minimum), 1) if 0 < minimum < 100 else 30.0
        wizard = self.create({
            "line_id": line.id, "product_tmpl_id": tmpl.id, "company_currency_id": move.company_id.currency_id.id,
            "cost": cost, "current_price": tmpl.list_price, "markup_percent": markup,
            "new_price": move.company_id.currency_id.round(cost * (1 + markup / 100.0))})
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": wizard.id,
                "view_mode": "form", "target": "new", "name": _("Adjust selling price")}

    @api.onchange("markup_percent")
    def _onchange_markup_percent(self):
        for wiz in self:
            wiz.new_price = wiz.company_currency_id.round(wiz.cost * (1 + wiz.markup_percent / 100.0))

    def action_confirm(self):
        """Set the selling price. Only this click does it; the website shows the new price from then on."""
        self.ensure_one()
        self.line_id.move_id._bill_ai_check_rights()
        if self.new_price <= 0:
            raise UserError(_("The new selling price must be positive."))
        tmpl = self.product_tmpl_id.sudo()
        old = tmpl.list_price
        tmpl.list_price = self.new_price
        tmpl.message_post(
            body=Markup(_("Selling price changed from %(old)s to %(new)s after the purchase price of bill %(move)s, "
                          "by %(who)s.") % {"old": old, "new": self.new_price, "move": self.line_id.move_id._bill_link(),
                                            "who": escape(self.env.user.name)}),
            subtype_xmlid="mail.mt_note", partner_ids=[])
        self.line_id.move_id._bill_check_margins()
        self.line_id.move_id._bill_ai_refresh_flags()
        return {"type": "ir.actions.act_window_close"}
