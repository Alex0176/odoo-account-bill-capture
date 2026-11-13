# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

MAX_FILES = 10
MAX_AI_FILES = 3        # a reading takes a while; more files are stored and can be read with the button


class BillCaptureController(http.Controller):

    def _allowed(self):
        # With the website installed the language follows the website / browser, not the user:
        # the page is an extension of the backend, so it speaks the language of the user.
        if request.env.user.lang:
            request.update_context(lang=request.env.user.lang)
        return request.env.user.has_group("account.group_account_invoice")

    def _page(self, results=None, status=200):
        company = request.env.company
        values = {
            "results": results or [], "mode": company.bill_capture_mode,
            "max_mb": round(request.env["account.move"]._bill_max_bytes() / 1048576, 1),
            "max_files": MAX_FILES, "max_ai": MAX_AI_FILES, "company": company.name,
            "backend_url": "/odoo",
            "t": {
                "title": _("Capture a vendor bill"),
                "only_store": _("Only store"),
                "read_ai": _("Read with AI"),
                "send": _("Send"),
                "open": _("Open"),
                "back": _("Back to Odoo"),
                "hint": _("PDF, JPEG or PNG, at most %(mb)s MB each, up to %(n)s files. Each file becomes a draft "
                          "vendor bill; nothing is posted.", mb=round(request.env["account.move"]._bill_max_bytes() / 1048576, 1),
                          n=MAX_FILES),
            },
        }
        return request.render("account_bill_capture.capture_page", values, status=status)

    @http.route("/bill-capture", type="http", auth="user", methods=["GET"])
    def page(self, **kw):
        if not self._allowed():
            return request.make_response(_("Not allowed."), status=403)
        return self._page()

    @http.route("/bill-capture/upload", type="http", auth="user", methods=["POST"], csrf=True)
    def upload(self, **post):
        if not self._allowed():
            return request.make_response(_("Not allowed."), status=403)
        use_ai = post.get("how") == "ai" and request.env.company.bill_capture_mode != "off"
        uploads = [f for f in request.httprequest.files.getlist("files") if f and f.filename]
        results = []
        if not uploads:
            results.append({"name": "", "ok": False, "message": _("No file was chosen.")})
        for index, upload in enumerate(uploads[:MAX_FILES]):
            read_now = use_ai and index < MAX_AI_FILES
            try:
                move, message = request.env["account.move"]._bill_capture_store(
                    upload.filename, upload.read(), ai=read_now)
                if use_ai and not read_now:
                    message = _("Stored. Open it and use 'Read with AI' (at most %s files are read per upload).",
                                MAX_AI_FILES)
                results.append({"name": upload.filename, "ok": True, "message": message,
                                "url": "/odoo/action-account.action_move_in_invoice_type/%s" % move.id})
            except (UserError, AccessError) as exc:
                results.append({"name": upload.filename, "ok": False, "message": str(exc)})
        for upload in uploads[MAX_FILES:]:
            results.append({"name": upload.filename, "ok": False,
                            "message": _("At most %s files per upload.", MAX_FILES)})
        return self._page(results)
