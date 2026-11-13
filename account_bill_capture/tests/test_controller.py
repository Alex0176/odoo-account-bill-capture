# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
from odoo import http
from odoo.tests.common import HttpCase, tagged

from .common import HEIC, JPEG, PDF, PNG, ScriptedBillProvider, answer
from odoo.addons.ai_translation_consensus import providers


@tagged("post_install", "-at_install")
class TestUpload(HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        providers.REGISTRY["scripted_bill"] = ScriptedBillProvider
        cls.addClassCleanup(providers.REGISTRY.pop, "scripted_bill", None)
        cls.company = cls.env.company
        cls.company.bill_capture_mode = "manual"
        cls.env["ai.translation.provider"].search([]).write({"active": False})
        cls.provider = cls.env["ai.translation.provider"].create({
            "name": "Scripted reader A", "kind": "scripted_bill", "is_local": True,
            "purpose_ids": [(6, 0, cls.env["ai.translation.purpose"].search(
                [("code", "in", ["invoice_manual", "invoice_auto"])]).ids)]})
        cls.env["res.users"].create({
            "name": "Upload accountant", "login": "upload_acc", "password": "upload_acc",
            "group_ids": [(6, 0, [cls.env.ref("account.group_account_invoice").id])]})
        cls.env["res.users"].create({
            "name": "Upload employee", "login": "upload_emp", "password": "upload_emp",
            "group_ids": [(6, 0, [cls.env.ref("base.group_user").id])]})
        journal = cls.env["account.journal"].search([("type", "=", "purchase"), ("company_id", "=", cls.company.id)], limit=1)
        if not journal:
            journal = cls.env["account.journal"].create({
                "name": "Test purchases", "code": "TPUR", "type": "purchase", "company_id": cls.company.id})
        cls.journal = journal

    def setUp(self):
        super().setUp()
        ScriptedBillProvider.calls = []
        ScriptedBillProvider.raises = None
        ScriptedBillProvider.reply = answer()

    def post_files(self, files, how="store"):
        return self.url_open("/bill-capture/upload", data={
            "csrf_token": http.Request.csrf_token(self), "how": how},
            files=[("files", (name, content, mime)) for name, content, mime in files])

    def test_the_page_needs_a_login_and_the_group(self):
        self.assertIn("login", self.url_open("/bill-capture", allow_redirects=True).url)
        self.authenticate("upload_emp", "upload_emp")
        self.assertEqual(self.url_open("/bill-capture").status_code, 403)
        self.assertEqual(self.post_files([("a.pdf", PDF, "application/pdf")]).status_code, 403)
        self.assertFalse(self.env["account.move"].search([("attachment_ids", "!=", False), ("create_uid.login", "=", "upload_emp")]))

    def test_the_page_is_plain_html_for_a_phone(self):
        self.authenticate("upload_acc", "upload_acc")
        page = self.url_open("/bill-capture").text
        self.assertIn('capture="environment"', page)
        self.assertIn('accept="image/*,application/pdf"', page)
        self.assertIn("multiple", page)
        self.assertIn("Only store", page)
        self.assertIn("Read with AI", page)
        self.assertNotIn("<script", page)                                # no JS framework, no JS at all
        self.company.bill_capture_mode = "off"
        self.assertNotIn("Read with AI", self.url_open("/bill-capture").text)
        self.company.bill_capture_mode = "manual"

    def test_every_file_becomes_a_draft_with_its_attachment(self):
        self.authenticate("upload_acc", "upload_acc")
        before = self.env["account.move"].search_count([("move_type", "=", "in_invoice")])
        response = self.post_files([("one.pdf", PDF, "application/pdf"), ("two.jpg", JPEG, "image/jpeg"),
                                    ("three.png", PNG, "image/png")])
        self.assertEqual(response.status_code, 200)
        moves = self.env["account.move"].search([("move_type", "=", "in_invoice")], order="id desc", limit=3)
        self.assertEqual(self.env["account.move"].search_count([("move_type", "=", "in_invoice")]), before + 3)
        for move in moves:
            self.assertEqual((move.state, move.journal_id), ("draft", self.journal))
            attachment = self.env["ir.attachment"].search([("res_model", "=", "account.move"), ("res_id", "=", move.id)])
            self.assertEqual(len(attachment), 1)
            self.assertEqual(move.message_main_attachment_id, attachment)      # the preview shows it
        self.assertFalse(ScriptedBillProvider.calls)                              # only stored

    def test_wrong_types_heic_and_big_files_are_refused_with_a_hint(self):
        self.authenticate("upload_acc", "upload_acc")
        before = self.env["account.move"].search_count([])
        page = self.post_files([("photo.heic", HEIC, "image/heic"), ("fake.pdf", b"just text", "application/pdf"),
                               ("macro.docx", b"PK\x03\x04....", "application/vnd.openxmlformats")]).text
        self.assertIn("HEIC", page)
        self.assertIn("not a PDF, JPEG or PNG", page)
        self.assertEqual(self.env["account.move"].search_count([]), before)
        self.env["ir.config_parameter"].set_param("account_bill_capture.max_file_mb", "0.001")
        page = self.post_files([("big.pdf", PDF + b"0" * 5000, "application/pdf")]).text
        self.assertIn("larger than", page)
        self.assertEqual(self.env["account.move"].search_count([]), before)

    def test_no_file_is_a_message_not_an_error(self):
        self.authenticate("upload_acc", "upload_acc")
        response = self.url_open("/bill-capture/upload", data={"csrf_token": http.Request.csrf_token(self), "how": "store"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("No file was chosen", response.text)

    def test_the_csrf_token_is_required(self):
        self.authenticate("upload_acc", "upload_acc")
        response = self.url_open("/bill-capture/upload", data={"how": "store"},
                                 files=[("files", ("a.pdf", PDF, "application/pdf"))])
        self.assertEqual(response.status_code, 400)

    def test_read_with_ai_reads_the_first_files_only(self):
        self.authenticate("upload_acc", "upload_acc")
        files = [("f%d.pdf" % i, PDF, "application/pdf") for i in range(5)]
        page = self.post_files(files, how="ai").text
        self.assertEqual(len(ScriptedBillProvider.calls), 3)
        self.assertIn("at most 3 files are read", page)
        self.assertIn("Proposal made", page)

    def test_read_with_ai_is_ignored_when_the_mode_is_off(self):
        self.authenticate("upload_acc", "upload_acc")
        self.company.bill_capture_mode = "off"
        self.post_files([("a.pdf", PDF, "application/pdf")], how="ai")
        self.assertFalse(ScriptedBillProvider.calls)
        self.company.bill_capture_mode = "manual"
