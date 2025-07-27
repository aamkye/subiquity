# Copyright 2020 Canonical, Ltd.
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
import inspect
import json
import logging
import os
import signal
from typing import Callable, Optional, Union

import urwid
import yaml

from subiquitycore.async_helpers import run_bg_task
from subiquitycore.core import Application
from subiquitycore.palette import PALETTE_COLOR, PALETTE_MONO
from subiquitycore.screen import make_screen
from subiquitycore.tuicontroller import Skip
from subiquitycore.ui.frame import SubiquityCoreUI
from subiquitycore.ui.utils import LoadingDialog
from subiquitycore.utils import astart_command
from subiquitycore.view import BaseView

log = logging.getLogger("subiquitycore.tui")


def extend_dec_special_charmap():
    urwid.escape.DEC_SPECIAL_CHARMAP.update(
        {
            ord("\N{BLACK RIGHT-POINTING SMALL TRIANGLE}"): ">",
            ord("\N{BLACK LEFT-POINTING SMALL TRIANGLE}"): "<",
            ord("\N{BLACK DOWN-POINTING SMALL TRIANGLE}"): "v",
            ord("\N{BLACK UP-POINTING SMALL TRIANGLE}"): "^",
            ord("\N{CHECK MARK}"): "*",
            ord("\N{CIRCLED WHITE STAR}"): "*",
            ord("\N{BULLET}"): "*",
            ord("\N{LOWER HALF BLOCK}"): "=",
            ord("\N{UPPER HALF BLOCK}"): "=",
            ord("\N{FULL BLOCK}"): urwid.escape.DEC_SPECIAL_CHARMAP[
                ord("\N{BOX DRAWINGS LIGHT VERTICAL}")
            ],
        }
    )


# When waiting for something of unknown duration, block the UI for at
# most this long before showing an indication of progress.
MAX_BLOCK_TIME = 0.1
# If an indication of progress is shown, show it for at least this
# long to avoid excessive flicker in the UI.
MIN_SHOW_PROGRESS_TIME = 1.0


class TuiApplication(Application):
    make_ui = SubiquityCoreUI

    def __init__(self, opts):
        super().__init__(opts)
        self.ui = self.make_ui()

        self.answers = {}
        if opts.answers is not None:
            self.answers = yaml.safe_load(opts.answers.read())
            log.debug("Loaded answers %s", self.answers)
            if not opts.dry_run:
                open("/run/casper-no-prompt", "w").close()

        self.rich_mode = None
        self.urwid_loop = None
        self.cur_screen = None
        self.fg_proc = None

    async def run_command_in_foreground(
        self, cmd, before_hook=None, after_hook=None, **kw
    ):
        if self.fg_proc is not None:
            raise Exception("cannot run two fg processes at once")
        screen = self.urwid_loop.screen

        async def _run():
            self.fg_proc = proc = await astart_command(
                cmd, stdin=None, stdout=None, stderr=None, **kw
            )
            await proc.communicate()
            self.fg_proc = None
            # One of the main use cases for this function is to run interactive
            # bash in a subshell. Interactive bash of course creates a process
            # group for itself and sets it as the foreground process group for
            # the controlling terminal. Usually on exit, our process group
            # becomes the foreground process group again but when the subshell
            # is killed for some reason it does not. This causes the tcsetattr
            # that screen.start() does to either cause SIGTTOU to be sent, or
            # if that is ignored (either because there is no shell around to do
            # job control things or we are ignoring it) fail with EIO. So we
            # force our process group back into the foreground with this
            # call. That's not quite enough though because tcsetpgrp *also*
            # causes SIGTTOU to be sent to a background process that calls it,
            # but fortunately if we ignore that (done in start_urwid below),
            # the call still succeeds.
            #
            # I would now like a drink.
            os.tcsetpgrp(0, os.getpgrp())
            screen.start()
            if after_hook is not None:
                after_hook()

        screen.stop()
        urwid.emit_signal(screen, urwid.display_common.INPUT_DESCRIPTORS_CHANGED)
        if before_hook is not None:
            before_hook()

        await _run()

    async def make_view_for_controller(
        self, new
    ) -> Union[BaseView, Callable[[], BaseView]]:
        new.context.enter("starting UI")
        if self.opts.screens and new.name not in self.opts.screens:
            raise Skip
        try:
            maybe_view = new.make_ui()
            if inspect.iscoroutine(maybe_view):
                view = await maybe_view
            else:
                view = maybe_view
        except Skip:
            new.context.exit("(skipped)")
            raise
        else:
            self.cur_screen = new
            return view

    async def _wait_with_indication(self, awaitable, show, hide=None):
        """Wait for something but tell the user if it takes a while.

        When waiting for something that can take an unknown length of
        time, we want to tell the user if it takes more than a moment
        (defined as MAX_BLOCK_TIME) but make sure that we display any
        indication for long enough that the UI is not flickering
        incomprehensibly (MIN_SHOW_PROGRESS_TIME).
        """
        min_show_task = None

        async def _show():
            await asyncio.sleep(MAX_BLOCK_TIME)
            self.ui.block_input = False
            nonlocal min_show_task
            min_show_task = asyncio.create_task(asyncio.sleep(MIN_SHOW_PROGRESS_TIME))
            await show()

        self.ui.block_input = True
        show_task = asyncio.create_task(_show())
        try:
            result = await awaitable
        finally:
            if min_show_task:
                await min_show_task
                if hide is not None:
                    await hide()
            else:
                self.ui.block_input = False
                show_task.cancel()

        return result

    def show_progress(self):
        raise NotImplementedError

    async def wait_with_text_dialog(self, awaitable, message, *, can_cancel=False):
        ld = None

        task_to_cancel = None
        if can_cancel:
            if not isinstance(awaitable, asyncio.Task):
                orig = awaitable

                async def w():
                    return await orig

                awaitable = task_to_cancel = asyncio.create_task(w())
            else:
                task_to_cancel = None

        async def show_load():
            nonlocal ld
            ld = LoadingDialog(self, message, task_to_cancel)
            self.ui.body.show_overlay(ld, width=ld.width)
            await self.redraw_screen()

        async def hide_load():
            ld.close()
            await self.redraw_screen()

        return await self._wait_with_indication(awaitable, show_load, hide_load)

    async def wait_with_progress(self, awaitable):
        async def show_progress():
            self.show_progress()
            await self.redraw_screen()

        return await self._wait_with_indication(awaitable, show_progress)

    async def _move_screen(
        self, increment, coro
    ) -> Optional[Union[BaseView, Callable[[], BaseView]]]:
        if coro is not None:
            await coro
        old, self.cur_screen = self.cur_screen, None
        if old is not None:
            old.context.exit("completed")
            old.end_ui()
        cur_index = self.controllers.index
        while True:
            self.controllers.index += increment
            if self.controllers.index < 0:
                self.controllers.index = cur_index
                return None
            if self.controllers.index >= len(self.controllers.instances):
                self.exit()
                return None
            new = self.controllers.cur
            try:
                return await self.make_view_for_controller(new)
            except Skip:
                log.debug("skipping screen %s", new.name)
                continue
            except Exception:
                self.controllers.index = cur_index
                raise

    async def move_screen(self, increment, coro):
        view_or_callable = await self.wait_with_progress(
            self._move_screen(increment, coro)
        )
        if view_or_callable is not None:
            if callable(view_or_callable):
                view = view_or_callable()
            else:
                view = view_or_callable
            self.ui.set_body(view)

    async def redraw_screen(self):
        if self.urwid_loop is None:
            # This should only happen very early on ; but there is probably no
            # point calling redraw_screen that early, right?
            return
        if not self.urwid_loop.screen.started:
            # This can happen if a foreground process (e.g., bash debug shell)
            # is running or if the installer is shutting down.
            return
        self.urwid_loop.draw_screen()

    async def next_screen(self, coro=None):
        await self.move_screen(1, coro)

    async def prev_screen(self):
        await self.move_screen(-1, None)

    async def select_initial_screen(self):
        await self.next_screen()

    def request_next_screen(self, coro=None, *, redraw=True):
        async def next_screen():
            await self.next_screen(coro)
            if redraw:
                await self.redraw_screen()

        run_bg_task(next_screen())

    def request_prev_screen(self, *, redraw=True):
        async def prev_screen():
            await self.prev_screen()
            if redraw:
                await self.redraw_screen()

        run_bg_task(prev_screen())

    def request_screen_redraw(self):
        run_bg_task(self.redraw_screen())

    def set_rich(self, rich):
        if rich == self.rich_mode:
            return
        if rich:
            urwid.util.set_encoding("utf-8")
            new_palette = PALETTE_COLOR
            self.rich_mode = True
        else:
            urwid.util.set_encoding("ascii")
            new_palette = PALETTE_MONO
            self.rich_mode = False
        if self.opts.run_on_serial:
            rich_mode_file = "rich-mode-serial"
        else:
            rich_mode_file = "rich-mode-tty"
        with open(self.state_path(rich_mode_file), "w") as fp:
            json.dump(self.rich_mode, fp)
        urwid.CanvasCache.clear()
        self.urwid_loop.screen.register_palette(new_palette)
        self.urwid_loop.screen.clear()

    def toggle_rich(self):
        self.set_rich(not self.rich_mode)

    def unhandled_input(self, key):
        if self.opts.dry_run and key == "ctrl x":
            self.exit()
        elif key == "f3":
            self.urwid_loop.screen.clear()
        elif self.opts.run_on_serial and key in ["ctrl t", "f4"]:
            self.toggle_rich()

    def extra_urwid_loop_args(self):
        return {}

    def make_screen(self, inputf=None, outputf=None):
        return make_screen(self.opts.ascii, inputf, outputf)

    def get_initial_rich_mode(self) -> bool:
        """Return the initial value for rich-mode, either loaded from an
        exising state file or automatically determined. True means rich mode
        and False means basic mode."""
        if self.opts.run_on_serial:
            rich_mode_file = "rich-mode-serial"
        else:
            rich_mode_file = "rich-mode-tty"
        try:
            fp = open(self.state_path(rich_mode_file))
        except FileNotFoundError:
            pass
        else:
            with fp:
                return json.load(fp)

        try:
            # During the 23.10 development cycle, there was only one rich-mode
            # state file. Let's handle the scenario where we just snap
            # refresh-ed from a pre 23.10 release.
            # Once mantic is EOL, let's remove this code.
            fp = open(self.state_path("rich-mode"))
        except FileNotFoundError:
            pass
        else:
            with fp:
                return json.load(fp)

        # By default, basic on serial - rich otherwise.
        return not self.opts.run_on_serial

    async def start_urwid(self, input=None, output=None):
        # This stops the tcsetpgrp call in run_command_in_foreground from
        # suspending us. See the rant there for more details.
        signal.signal(signal.SIGTTOU, signal.SIG_IGN)
        screen = self.make_screen(input, output)
        screen.register_palette(PALETTE_COLOR)
        self.urwid_loop = urwid.MainLoop(
            self.ui,
            screen=screen,
            handle_mouse=False,
            pop_ups=True,
            unhandled_input=self.unhandled_input,
            event_loop=urwid.AsyncioEventLoop(loop=asyncio.get_running_loop()),
            **self.extra_urwid_loop_args(),
        )
        extend_dec_special_charmap()
        self.set_rich(self.get_initial_rich_mode())
        self.urwid_loop.start()
        await self.select_initial_screen()
        await self.redraw_screen()

    async def start(self, start_urwid=True):
        await super().start()
        if start_urwid:
            await self.start_urwid()

    async def run(self):
        try:
            await super().run()
        finally:
            if self.urwid_loop is not None:
                self.urwid_loop.stop()
