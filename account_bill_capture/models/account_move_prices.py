# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Vendor prices on the product (standard ``product.supplierinfo``) and the margin warning."""
import json
import logging

from markupsafe import Markup, escape

from odoo import _, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_is_zero
from odoo.tools.misc import formatLang

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    # ------------------------------------------------------------------ posting hook
    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        for move in posted.filtered(lambda m: m.move_type == "in_invoice"):
            company = move.company_id
            if company.bill_supplier_price_update != "post" and not company.bill_margin_check:
                continue
            try:
                with self.env.cr.savepoint():
                    if company.bill_supplier_price_update == "post":
                        move._bill_update_supplier_prices("post")
                    if company.bill_margin_check:
                        move._bill_check_margins()
                    move._bill_ai_refresh_flags()
            except Exception:  # noqa: BLE001 - the vendor prices never stop a posting
                _logger.exception("Bill capture: vendor prices of move %s were not updated", move.id)
        return posted

    # ------------------------------------------------------------------ vendor prices
    def _bill_price_lines(self):
        """The lines whose price may become a vendor price: products only; no credit notes, no
        zero prices or quantities."""
        self.ensure_one()
        if self.move_type != "in_invoice":
            return self.env["account.move.line"]
        return self.invoice_line_ids.filtered(
            lambda l: l.display_type == "product" and l.product_id and l.quantity > 0 and l.price_unit > 0
            and (l.discount or 0.0) < 100)

    def _bill_net_price(self, line):
        """Price of one unit of the line after discount (move currency, unit of the line)."""
        return line.price_unit * (1 - (line.discount or 0.0) / 100.0)

    def _bill_price_plan(self, line):
        """What the price of ``line`` means for the vendor list of its product.

        ``{"action": "same" | "change" | "create", ...}`` with the seller, old and new price in the
        currency and unit of the vendor entry."""
        self.ensure_one()
        product = line.product_id
        partner = self.commercial_partner_id
        if not partner:
            return None
        date = self.invoice_date or fields.Date.context_today(self)
        company = self.company_id
        net = self._bill_net_price(line)
        sellers = product.product_tmpl_id.seller_ids.filtered(
            lambda s: s.partner_id.commercial_partner_id == partner and (not s.product_id or s.product_id == product)
            and (not s.company_id or s.company_id == company)
            and (not s.date_start or s.date_start <= date) and (not s.date_end or s.date_end >= date))
        same_currency = sellers.filtered(lambda s: s.currency_id == self.currency_id)
        sellers = same_currency or sellers

        def seller_qty(s):
            return line.product_uom_id._compute_quantity(line.quantity, s.product_uom_id or line.product_uom_id)

        fitting = sellers.filtered(lambda s: s.min_qty <= seller_qty(s) + 1e-9)
        seller = max(fitting, key=lambda s: (s.min_qty, -s.sequence), default=False)
        precision = self.env["decimal.precision"].precision_get("Product Price")
        tiers = sellers.filtered(lambda s: s.min_qty > 1)
        if not seller:
            min_qty = 1.0
            return {"action": "create", "line": line, "product": product, "price": net, "min_qty": min_qty,
                    "currency": self.currency_id, "uom": line.product_uom_id, "old": 0.0,
                    "tiered": bool(tiers)}
        unit = seller.product_uom_id or line.product_uom_id
        new = line.product_uom_id._compute_price(net, unit)
        if seller.currency_id and seller.currency_id != self.currency_id:
            new = self.currency_id._convert(new, seller.currency_id, company, date, round=False)
        old = seller.price * (1 - (seller.discount or 0.0) / 100.0)
        if float_is_zero(new - old, precision_digits=precision):
            return {"action": "same", "line": line, "product": product, "seller": seller, "price": new, "old": old}
        return {"action": "change", "line": line, "product": product, "seller": seller, "price": new, "old": old,
                "currency": seller.currency_id or self.currency_id}

    def _bill_update_supplier_prices(self, origin, confirmed_logs=None):
        """Bring the vendor prices of the products in line with this bill. Returns the number of changes.

        ``confirmed_logs``: pending suggestions that a person confirmed (the limit does not apply)."""
        self.ensure_one()
        company = self.company_id
        Log = self.env["account.bill.capture.log"].sudo()
        limit = company.bill_price_change_limit
        changed = 0
        confirmed = {l.line_id.id: l for l in (confirmed_logs or Log.browse())}
        for line in self._bill_price_lines():
            plan = self._bill_price_plan(line)
            if not plan:
                continue
            pending_log = confirmed.get(line.id)
            if plan["action"] == "same":
                self._bill_touch_seller(plan["seller"], line)
                if pending_log:
                    pending_log.state = "dismissed"
                continue
            if plan["action"] == "change" and line.id not in confirmed:
                old = plan["old"]
                if old > 0 and abs(plan["price"] - old) / old * 100.0 > limit:
                    self._bill_log_pending(plan, origin)
                    continue
            self._bill_execute_plan(plan, origin, pending_log)
            changed += 1
        return changed

    def _bill_touch_seller(self, seller, line):
        seller.sudo().write({"bill_last_move_id": self.id, "bill_last_date": self.invoice_date or fields.Date.today(),
                             "bill_last_price": seller.price})

    def _bill_log_pending(self, plan, origin):
        Log = self.env["account.bill.capture.log"].sudo()
        line = plan["line"]
        existing = Log.search([("move_id", "=", self.id), ("line_id", "=", line.id), ("kind", "=", "price"),
                               ("state", "=", "pending")], limit=1)
        vals = {"kind": "price", "state": "pending", "origin": origin, "move_id": self.id,
                "company_id": self.company_id.id, "line_id": line.id, "product_id": plan["product"].product_tmpl_id.id,
                "seller_id": plan["seller"].id, "partner_id": self.commercial_partner_id.id,
                "old_price": plan["old"], "new_price": plan["price"], "currency_id": plan["currency"].id}
        if existing:
            existing.write(vals)
        else:
            Log.create(vals)
        plan["product"].product_tmpl_id.sudo().message_post(
            body=Markup(_("Suggested vendor price %(old)s to %(new)s (%(partner)s, bill %(move)s). The change is "
                          "large, so it is not applied: confirm it on the bill.") % {
                "old": plan["old"], "new": plan["price"], "partner": escape(self.commercial_partner_id.display_name),
                "move": self._bill_link()}),
            subtype_xmlid="mail.mt_note", partner_ids=[])

    def _bill_link(self):
        return Markup('<a href="#" data-oe-model="account.move" data-oe-id="%s">%s</a>') % (
            self.id, escape(self.display_name or self.ref or self.id))

    def _bill_execute_plan(self, plan, origin, pending_log=None):
        """Write the vendor price; log it so that it can be undone; note it on the product."""
        self.ensure_one()
        company = self.company_id
        line, product = plan["line"], plan["product"]
        Log = self.env["account.bill.capture.log"].sudo()
        Seller = self.env["product.supplierinfo"].sudo()
        date = self.invoice_date or fields.Date.context_today(self)
        who = _("AI capture") if origin == "instant" else self.env.user.name
        old_values = {}
        if plan["action"] == "create":
            sequence = max(product.product_tmpl_id.seller_ids.mapped("sequence") or [0]) + 1
            number = line.bill_ai_item_number or False
            seller = Seller.create({
                "partner_id": self.commercial_partner_id.id, "product_tmpl_id": product.product_tmpl_id.id,
                "product_id": product.id if product.product_tmpl_id.product_variant_count > 1 else False,
                "price": plan["price"], "currency_id": plan["currency"].id, "min_qty": plan["min_qty"],
                "product_uom_id": plan["uom"].id, "product_code": number,
                "product_name": (line.name or "")[:200] if number else False, "company_id": company.id,
                "sequence": sequence, "date_start": False,
                "bill_last_move_id": self.id, "bill_last_date": date, "bill_last_price": plan["price"]})
            created = True
        else:
            seller = plan["seller"].sudo()
            old_values = {"price": seller.price, "discount": seller.discount, "date_start": str(seller.date_start or ""),
                          "date_end": str(seller.date_end or ""), "bill_last_move_id": seller.bill_last_move_id.id,
                          "bill_last_date": str(seller.bill_last_date or ""), "bill_last_price": seller.bill_last_price}
            vals = {"price": plan["price"], "discount": 0.0, "bill_last_move_id": self.id,
                    "bill_last_date": date, "bill_last_price": plan["price"]}
            if seller.date_start:
                vals["date_start"] = date
            if seller.date_end and seller.date_end < date:
                vals["date_end"] = False
            seller.write(vals)
            created = False
        std = {"std_changed": False}
        tmpl = product.product_tmpl_id
        if company.bill_update_standard_price and not getattr(tmpl, "is_storable", False):
            cost = line.product_uom_id._compute_price(self._bill_net_price(line), product.uom_id)
            cost = self.currency_id._convert(cost, company.currency_id, company, date, round=False)
            prod = product.sudo().with_company(company)
            std = {"std_changed": True, "old_standard_price": prod.standard_price, "new_standard_price": cost}
            prod.standard_price = cost
        vals = dict(kind="price", state="applied", origin=origin, move_id=self.id, company_id=company.id,
                    line_id=line.id, product_id=tmpl.id, seller_id=seller.id,
                    partner_id=self.commercial_partner_id.id, old_price=plan["old"], new_price=plan["price"],
                    currency_id=plan["currency"].id, created_seller=created, old_values=json.dumps(old_values),
                    **std)
        if pending_log:
            pending_log.write(vals)
            log = pending_log
        else:
            log = Log.create(vals)
        tmpl.sudo().message_post(
            body=Markup(_("Vendor price of %(partner)s: %(old)s to %(new)s %(currency)s, bill %(move)s, by %(who)s.")
                        % {"partner": escape(self.commercial_partner_id.display_name),
                           "old": plan["old"] if not created else _("(new entry)"), "new": plan["price"],
                           "currency": escape(plan["currency"].name), "move": self._bill_link(),
                           "who": escape(who)}),
            subtype_xmlid="mail.mt_note", partner_ids=[])
        return log

    def action_bill_confirm_price_changes(self):
        """Apply the vendor price changes that were too large to apply on their own."""
        self.ensure_one()
        self._bill_ai_check_rights()
        pending = self.env["account.bill.capture.log"].sudo().search(
            [("move_id", "=", self.id), ("kind", "=", "price"), ("state", "=", "pending")])
        if not pending:
            raise UserError(_("There is no price change to confirm."))
        self._bill_update_supplier_prices("confirm", confirmed_logs=pending)
        self._bill_ai_refresh_flags()
        return True

    def action_bill_dismiss_price_changes(self):
        self.ensure_one()
        self._bill_ai_check_rights()
        self.env["account.bill.capture.log"].sudo().search(
            [("move_id", "=", self.id), ("kind", "=", "price"), ("state", "=", "pending")]).write({"state": "dismissed"})
        self._bill_ai_refresh_flags()
        return True

    def action_bill_revert_price_changes(self):
        """Undo the vendor price changes this bill made (button 'Undo price changes')."""
        self.ensure_one()
        self._bill_ai_check_rights()
        logs = self.env["account.bill.capture.log"].sudo().search(
            [("move_id", "=", self.id), ("kind", "=", "price"), ("state", "=", "applied")], order="id desc")
        if not logs:
            raise UserError(_("This bill changed no vendor price."))
        skipped = 0
        for log in logs:
            seller = log.seller_id
            tmpl = log.product_id
            if seller and abs(seller.price - log.new_price) > 1e-6:
                skipped += 1            # somebody changed it again since: leave it alone
                continue
            if log.created_seller:
                seller.sudo().unlink()
            elif seller:
                old = json.loads(log.old_values or "{}")
                seller.sudo().write({
                    "price": old.get("price", seller.price), "discount": old.get("discount", 0.0),
                    "date_start": old.get("date_start") or False, "date_end": old.get("date_end") or False,
                    "bill_last_move_id": old.get("bill_last_move_id") or False,
                    "bill_last_date": old.get("bill_last_date") or False,
                    "bill_last_price": old.get("bill_last_price", 0.0)})
            if log.std_changed and tmpl:
                prod = tmpl.product_variant_id.sudo().with_company(log.company_id)
                prod.standard_price = log.old_standard_price
            if tmpl:
                tmpl.sudo().message_post(
                    body=Markup(_("Vendor price change of bill %(move)s undone by %(who)s.") % {
                        "move": self._bill_link(), "who": escape(self.env.user.name)}),
                    subtype_xmlid="mail.mt_note", partner_ids=[])
            log.state = "reverted"
        self._bill_ai_refresh_flags()
        if skipped:
            self.message_post(body=_("%s price changes were not undone because the price was changed again.", skipped),
                              subtype_xmlid="mail.mt_note", partner_ids=[])
        return True

    # ------------------------------------------------------------------ margin
    def _bill_cost_per_product_unit(self, line):
        """Purchase price of the line in company currency per unit of the product (its selling unit)."""
        self.ensure_one()
        date = self.invoice_date or fields.Date.context_today(self)
        net = self._bill_net_price(line)
        net = line.product_uom_id._compute_price(net, line.product_id.uom_id)
        return self.currency_id._convert(net, self.company_id.currency_id, self.company_id, date, round=False)

    def _bill_check_margins(self):
        """A warning for every position whose product sells below its new purchase price (or under the
        minimum margin). Selling prices are never changed here."""
        self.ensure_one()
        company = self.company_id
        if self.move_type != "in_invoice" or not company.bill_margin_check:
            return
        minimum = company.bill_min_margin_percent or 0.0
        cur = company.currency_id
        for line in self._bill_price_lines():
            tmpl = line.product_id.product_tmpl_id
            warning = False
            hint = False
            if tmpl.sale_ok and tmpl.list_price > 0:
                cost = self._bill_cost_per_product_unit(line)
                sale = tmpl.list_price
                margin = (sale - cost) / sale * 100.0
                if sale < cost - 1e-9:
                    warning = _("Selling price %(sale)s is below the purchase price %(cost)s: margin %(margin)d %%",
                                sale=formatLang(self.env, sale, currency_obj=cur),
                                cost=formatLang(self.env, cost, currency_obj=cur), margin=round(margin))
                elif minimum > 0 and margin < minimum:
                    warning = _("Margin %(margin)d %% is below the minimum of %(minimum)d %% "
                                "(selling price %(sale)s, purchase price %(cost)s)",
                                margin=round(margin), minimum=round(minimum),
                                sale=formatLang(self.env, sale, currency_obj=cur),
                                cost=formatLang(self.env, cost, currency_obj=cur))
            elif tmpl.sale_ok:
                hint = _("No selling price set")
            was = line.bill_ai_margin_warning
            line.write({"bill_ai_margin_warning": bool(warning), "bill_ai_margin_hint": warning or hint or False})
            if warning and not was:
                tmpl.sudo().message_post(
                    body=Markup("%s<br/>%s") % (escape(warning), self._bill_link()),
                    subtype_xmlid="mail.mt_note", partner_ids=[])
                activity_user = company.bill_margin_activity_user_id
                if activity_user:
                    summary = _("Selling price too low")
                    if not tmpl.activity_ids.filtered(lambda a: a.summary == summary and a.user_id == activity_user):
                        tmpl.sudo().with_context(mail_activity_quick_update=True).activity_schedule(
                            "mail.mail_activity_data_todo", summary=summary, note=warning, user_id=activity_user.id)
