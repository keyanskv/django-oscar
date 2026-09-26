import threading
from unittest import skipUnless

from django.core import exceptions
from django.db import connection
from django.test import TransactionTestCase, TestCase

from oscar.apps.voucher.models import Voucher
from oscar.test.factories import OrderFactory, UserFactory, VoucherFactory
from oscar.test.utils import run_concurrently


class TestSingleUseVoucherAvailabilityCheck(TestCase):

    def setUp(self):
        self.voucher = VoucherFactory(usage=Voucher.SINGLE_USE)
        self.user = UserFactory()
        self.order = OrderFactory()

    def test_available_before_first_use(self):
        is_available, _ = self.voucher.is_available_to_user(user=self.user)
        self.assertTrue(is_available)

    def test_unavailable_after_first_use(self):
        self.voucher.record_usage(self.order, self.user)
        self.voucher.refresh_from_db()
        is_available, message = self.voucher.is_available_to_user(user=self.user)
        self.assertFalse(is_available)
        self.assertIn("already been used", str(message))

    def test_record_usage_raises_on_second_attempt(self):
        order2 = OrderFactory()
        self.voucher.record_usage(self.order, self.user)
        with self.assertRaises(exceptions.ValidationError):
            self.voucher.record_usage(order2, self.user)

    def test_stale_num_orders_does_not_bypass_check(self):
        self.voucher.record_usage(self.order, self.user)
        stale = Voucher.objects.get(pk=self.voucher.pk)
        stale.num_orders = 0
        is_available, _ = stale.is_available_to_user(user=self.user)
        self.assertFalse(is_available)


@skipUnless(
    connection.vendor == "postgresql",
    "SELECT FOR UPDATE is not supported by SQLite",
)
class TestVoucherConcurrency(TransactionTestCase):

    def setUp(self):
        self.user = UserFactory()
        self.order = OrderFactory()

    def test_single_use_voucher_concurrent_redemption(self):
        voucher = VoucherFactory(usage=Voucher.SINGLE_USE)

        def worker():
            voucher.record_usage(self.order, self.user)

        errors = run_concurrently(worker, num_threads=2)

        voucher.refresh_from_db()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], exceptions.ValidationError)
        self.assertEqual(voucher.applications.count(), 1)
        self.assertEqual(voucher.num_orders, 1)

    def test_once_per_customer_voucher_concurrent_redemption(self):
        voucher = VoucherFactory(usage=Voucher.ONCE_PER_CUSTOMER)

        def worker():
            voucher.record_usage(self.order, self.user)

        errors = run_concurrently(worker, num_threads=2)

        voucher.refresh_from_db()
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], exceptions.ValidationError)
        self.assertEqual(voucher.applications.count(), 1)

    def test_once_per_customer_voucher_different_users(self):
        voucher = VoucherFactory(usage=Voucher.ONCE_PER_CUSTOMER)
        user2 = UserFactory()
        order2 = OrderFactory()

        def worker():
            name = threading.current_thread().name
            index = int(name.rsplit("-", 1)[1])
            if index % 2 == 0:
                voucher.record_usage(self.order, self.user)
            else:
                voucher.record_usage(order2, user2)

        errors = run_concurrently(worker, num_threads=2)

        voucher.refresh_from_db()
        self.assertEqual(len(errors), 0)
        self.assertEqual(voucher.applications.count(), 2)

    def test_multi_use_voucher_concurrent_redemptions(self):
        voucher = VoucherFactory(usage=Voucher.MULTI_USE)
        n = 5
        users = [UserFactory() for _ in range(n)]
        orders = [OrderFactory() for _ in range(n)]

        def worker():
            name = threading.current_thread().name
            index = int(name.rsplit("-", 1)[1])
            voucher.record_usage(orders[index], users[index])

        errors = run_concurrently(worker, num_threads=n)

        voucher.refresh_from_db()
        self.assertEqual(len(errors), 0)
        self.assertEqual(voucher.applications.count(), n)
        self.assertEqual(voucher.num_orders, n)
