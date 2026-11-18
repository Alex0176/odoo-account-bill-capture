# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    bill_recurring = fields.Boolean(
        string="Standing invoice (every month)", company_dependent=True,
        help="This supplier sends one standing invoice (e.g. rent) that is booked every month. When such a bill "
             "is read with AI, its old invoice date and its invoice number that exists already are accepted: the "
             "bill gets today's date and the number gets the month, e.g. 01/2023 (11/2026).")
