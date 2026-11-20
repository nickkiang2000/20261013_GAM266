"""PyPtt 2.3.16 text assembly with Down used throughout the article.

Preserve the SDK text extraction and timeout handling.
Signatures and pushes do not change the navigation key.
"""

import re

from PyPtt import command, connect_core, exceptions, log, screens
from PyPtt._api_util import expanded_screen


def get_content(api, post_mode: bool = True):
    api.Unconfirmed = False

    with expanded_screen(api):
        def is_unconfirmed_handler(screen):
            api.Unconfirmed = True

        if post_mode:
            cmd = command.enter * 2
        else:
            cmd = command.enter

        target_list = [

            connect_core.TargetUnit('本篇文章內容經站方授權之板務管理人員判斷有尚待證實之處', response=' ',
                                    handler=is_unconfirmed_handler),
            connect_core.TargetUnit(screens.Target.PostEnd, log_level=log.DEBUG, break_detect=True),
            connect_core.TargetUnit(screens.Target.InPost, log_level=log.DEBUG, break_detect=True),
            connect_core.TargetUnit(screens.Target.PostNoContent, log_level=log.DEBUG, break_detect=True),

            connect_core.TargetUnit(screens.Target.Animation, response='n'),
        ]

        line_from_pattern = re.compile(r'[\d]+~[\d]+')

        has_control_code = False
        control_code_mode = False
        content_start_exist = False
        content_start_jump = False
        content_start_jump_set = False

        first_page = True
        origin_post = []
        stop_dict = dict()
        new_content_part = ''
        get_line_b = 0
        max_line_b = 0
        stall = 0
        timeouts = 0
        while True:
            index = api.connect_core.send(cmd, target_list)
            if index == 3 or index == 4:
                return None, False
            if index < 0:

                timeouts += 1
                if timeouts < 3:
                    cmd = None
                    continue
                if api.connect_core.last_timeout_was_silent:
                    raise exceptions.ConnectionClosed()
                status_line = api.connect_core.get_screen_queue()[-1].split('\n')[-1]
                raise exceptions.UnknownError(f'get_content: unrecognized screen {status_line!r}')
            timeouts = 0

            last_screen = api.connect_core.get_screen_queue()[-1]
            lines = last_screen.split('\n')
            last_line = lines[-1]
            lines.pop()
            last_screen = '\n'.join(lines)

            if screens.Target.content_start in last_screen and not content_start_exist:
                content_start_exist = True

            if content_start_exist:
                if not content_start_jump_set:
                    if screens.Target.content_start not in last_screen:
                        content_start_jump = True
                        content_start_jump_set = True
                else:
                    content_start_jump = False

            pattern_result = line_from_pattern.search(last_line)
            if pattern_result is None:
                control_code_mode = True
                has_control_code = True
            else:
                last_read_line_list = pattern_result.group(0).split('~')
                last_read_line_a_temp = int(last_read_line_list[0])
                last_read_line_b_temp = int(last_read_line_list[1])

                if last_read_line_b_temp > max_line_b:
                    max_line_b, stall = last_read_line_b_temp, 0
                else:
                    stall += 1
                    if stall > 20:
                        raise exceptions.UnknownError(f'get_content: stuck at line {last_read_line_b_temp}')
                if control_code_mode:
                    last_read_line_a = last_read_line_a_temp - 1
                    last_read_line_b = last_read_line_b_temp - 1
                control_code_mode = False

            if first_page:
                first_page = False
                origin_post.append(last_screen)
            else:

                if not control_code_mode:

                    if last_read_line_a_temp in stop_dict:
                        new_content_part = '\n'.join(
                            lines[-stop_dict[last_read_line_a_temp]:])
                        stop_dict = dict()
                    else:
                        get_line_b = last_read_line_b_temp - last_read_line_b
                        if get_line_b > 0:

                            new_content_part = '\n'.join(lines[-get_line_b:])

                            if get_line_b <= 23:
                                if index == 1 and len(new_content_part) == get_line_b - 1:
                                    new_content_part = '\n'.join(lines[-(get_line_b * 2):])
                                elif origin_post and get_line_b + 1 <= len(lines):
                                    last_line_temp = origin_post[-1].strip()
                                    try_line = lines[-(get_line_b + 1)].strip()

                                    if not last_line_temp.endswith(try_line):
                                        new_content_part = try_line + '\n' + new_content_part
                            stop_dict = dict()
                        else:

                            if (last_read_line_b_temp + 1) not in stop_dict:
                                stop_dict[last_read_line_b_temp + 1] = 1
                            stop_dict[last_read_line_b_temp + 1] += 1

                            get_line_a = last_read_line_a_temp - last_read_line_a

                            if get_line_a > 0:

                                new_content_part = '\n'.join(lines[-get_line_a:])
                            else:
                                new_content_part = '\n'.join(lines)

                else:
                    new_content_part = lines[-1]

                origin_post.append(new_content_part)

                log.logger.debug('NewContentPart', new_content_part)

            if index == 1:
                if content_start_jump and len(new_content_part) == 0:
                    get_line_b += 1
                    new_content_part = '\n'.join(lines[-get_line_b:])

                    origin_post.pop()
                    origin_post.append(new_content_part)
                break

            if not control_code_mode:
                last_read_line_a = last_read_line_a_temp
                last_read_line_b = last_read_line_b_temp

            cmd = command.down

    origin_post = '\n'.join(origin_post)

    log.logger.debug('OriginPost', origin_post)

    return origin_post, has_control_code
