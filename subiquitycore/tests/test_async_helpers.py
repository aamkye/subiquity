# Copyright 2022 Canonical, Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import unittest
from unittest.mock import AsyncMock

from subiquitycore.async_helpers import (
    SingleInstanceTask,
    TaskAlreadyRunningError,
    exclusive,
)
from subiquitycore.tests.parameterized import parameterized


class TestSingleInstanceTask(unittest.IsolatedAsyncioTestCase):
    @parameterized.expand([(True, 2), (False, 1)])
    async def test_cancellable(self, cancel_restart, expected_call_count):
        async def fn():
            await asyncio.sleep(3)
            raise Exception("timeout")

        mock_fn = AsyncMock(side_effect=fn)
        sit = SingleInstanceTask(mock_fn, cancel_restart=cancel_restart)
        await sit.start()
        await asyncio.sleep(0.01)
        try:
            await sit.start()
        except TaskAlreadyRunningError:
            restarted = False
        else:
            restarted = True
        sit.task.cancel()
        self.assertEqual(expected_call_count, mock_fn.call_count)
        self.assertEqual(cancel_restart, restarted)


# previously, wait() may or may not have been safe to call, depending
# on if the task had actually been created yet.
class TestSITWait(unittest.IsolatedAsyncioTestCase):
    async def test_wait_started(self):
        async def fn():
            pass

        sit = SingleInstanceTask(fn)
        await sit.start()
        await asyncio.wait_for(sit.wait(), timeout=1.0)
        self.assertTrue(sit.done())

    async def test_wait_not_started(self):
        async def fn():
            self.fail("not supposed to be called")

        sit = SingleInstanceTask(fn)
        self.assertFalse(sit.done())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(sit.wait(), timeout=0.1)
        self.assertFalse(sit.done())


class TestExclusive(unittest.IsolatedAsyncioTestCase):
    async def test_concurrency(self):
        timeout = 0.1
        barrier = asyncio.Barrier(parties=2)

        async def f():
            async with barrier:
                pass

        await asyncio.wait_for(asyncio.gather(f(), f()), timeout=timeout)

        g = exclusive(f)

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(g(), g()), timeout=timeout)

        # This is the same as g, but just to show an example of the intended
        # usage.
        @exclusive
        async def e():
            async with barrier:
                pass

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.gather(e(), e()), timeout=timeout)
