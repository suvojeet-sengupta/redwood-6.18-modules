#!/usr/bin/env python3
# Xiaomi touch features for mainline goodix_berlin on redwood (GT9916), ported
# from Xiaomi's gt9916 driver:
# - double tap to wake (0xa6 / 0xcc -> KEY_WAKEUP) and single tap on AOD
#   (0xa6 / 0x4c -> KEY_GOTO), with the IC kept in gesture mode while the
#   screen is off or in doze
# - 240/480 Hz report rate (cmd 0xc0, sysfs switch_report_rate)
# - game mode and touch tuning (cmds 0x17 / 0x18) through xiaomi_touch modes,
#   and game mode alone from sysfs (game_mode)
# - palm detection (cmd 0x70, large-touch flag -> update_palm_sensor_value)
# - charger state sent to the IC (cmd 0xaf) from a power_supply notifier
# Build with xiaomi_touch's Module.symvers in KBUILD_EXTRA_SYMBOLS.
# Usage: xm_touch.py <dir with goodix_berlin_core.c> <xiaomi_touch.h> <panel_event_notifier.h>
import os
import shutil
import sys

if len(sys.argv) != 4:
    sys.exit("usage: xm_touch.py <dir with goodix_berlin_core.c> <xiaomi_touch.h> <panel_event_notifier.h>")

src_dir, xmh, pnh = sys.argv[1:4]
shutil.copy(xmh, os.path.join(src_dir, "xiaomi_touch.h"))
shutil.copy(pnh, os.path.join(src_dir, "panel_event_notifier.h"))
core_path = os.path.join(src_dir, "goodix_berlin_core.c")
with open(core_path, "r", encoding="utf-8") as f:
    src = f.read()


def sub(old: str, new: str) -> None:
    global src
    cnt = src.count(old)
    if cnt != 1:
        sys.exit(f"xm_touch: expected 1 match, got {cnt} for {old[:72]!r}")
    src = src.replace(old, new)


sub(
    """#include <linux/unaligned.h>

#include "goodix_berlin.h"
""",
    """#include <linux/module.h>
#include <linux/of.h>
#include <linux/power_supply.h>
#include <linux/unaligned.h>
#include <linux/workqueue.h>
#include <drm/drm_panel.h>

#include "goodix_berlin.h"
#include "panel_event_notifier.h"
#include "xiaomi_touch.h"
""",
)

sub(
    """#define GOODIX_BERLIN_REQUEST_EVENT\t\tBIT(6)
""",
    """#define GOODIX_BERLIN_REQUEST_EVENT\t\tBIT(6)
#define GOODIX_BERLIN_GESTURE_EVENT\t\tBIT(5)
#define GOODIX_BERLIN_LARGE_TOUCH_EVENT\t\tBIT(4)
#define GOODIX_BERLIN_GESTURE_DOUBLE_TAP\t0xcc
#define GOODIX_BERLIN_GESTURE_SINGLE_TAP\t0x4c

#define GOODIX_BERLIN_CMD_GESTURE\t\t0xa6
#define GOODIX_BERLIN_CMD_GAME_ON\t\t0x17
#define GOODIX_BERLIN_CMD_GAME_OFF\t\t0x18
#define GOODIX_BERLIN_CMD_PALM\t\t\t0x70
#define GOODIX_BERLIN_CMD_CHARGER\t\t0xaf
#define GOODIX_BERLIN_CMD_REPORT_RATE\t\t0xc0
#define GOODIX_BERLIN_CMD_BUF_LEN\t\t16
#define GOODIX_BERLIN_CMD_ACK_OK\t\t0x80
#define GOODIX_BERLIN_CMD_ACK_BUSY\t\t0x02
#define GOODIX_BERLIN_CMD_RETRY\t\t\t6

#define GOODIX_PANEL_ORIENTATION_90\t\t1
#define GOODIX_PANEL_ORIENTATION_270\t\t3
""",
)

sub(
    """\t/* Runtime parameters extracted from IC_INFO buffer  */
\tu32 touch_data_addr;
""",
    """\t/* Runtime parameters extracted from IC_INFO buffer  */
\tu32 touch_data_addr;
\tu32 cmd_addr;

\t/* Xiaomi Touch integration (DT2W/AOD, game DSP, palm, charger, 480Hz) */
\tstruct mutex dt2w_lock;
\tstruct delayed_work panel_work;
\tstruct work_struct blank_work;
\tstruct work_struct game_work;
\tstruct work_struct charger_work;
\tstruct work_struct reapply_work;
\tstruct notifier_block charger_notifier;
\tvoid *panel_cookie;
\tint panel_tries;
\tbool dt2w;
\tbool aod_status;
\tbool fod_icon_status;
\tbool nonui_status;
\tbool screen_off;
\tbool gesture_mode;
\tbool irq_wake;
\tbool palm_status;
\tbool palm_active;
\tbool charger_on;
\tbool report_rate;
\tint touch_mode[Touch_Mode_NUM][VALUE_TYPE_SIZE];
""",
)

sub(
    """\tmisc = (struct goodix_berlin_ic_info_misc *)&data[offset];
\tcd->touch_data_addr = le32_to_cpu(misc->touch_data_addr);
""",
    """\tmisc = (struct goodix_berlin_ic_info_misc *)&data[offset];
\tcd->touch_data_addr = le32_to_cpu(misc->touch_data_addr);
\tcd->cmd_addr = le32_to_cpu(misc->cmd_addr);
""",
)

sub(
    """static irqreturn_t goodix_berlin_irq(int irq, void *data)
{""",
    """/*
 * Command packet layout: { state, ack, len, cmd, data..., checksum_le16 }.
 * Checksum covers bytes [2 .. len - 1] and is written at &buf[len].
 */
static int goodix_berlin_send_cmd(struct goodix_berlin_core *cd, u8 cmd,
\t\t\t\t  const u8 *data, u8 data_len)
{
\tu8 buf[GOODIX_BERLIN_CMD_BUF_LEN] = { 0 };
\tu8 ack[GOODIX_BERLIN_CMD_BUF_LEN];
\tu16 sum = 0;
\tint retry, i, error;

\tif (!cd->cmd_addr || data_len > GOODIX_BERLIN_CMD_BUF_LEN - 6)
\t\treturn -EINVAL;

\tbuf[2] = data_len + 4;
\tbuf[3] = cmd;
\tmemcpy(&buf[4], data, data_len);
\tfor (i = 2; i < buf[2]; i++)
\t\tsum += buf[i];
\tput_unaligned_le16(sum, &buf[buf[2]]);

\tfor (retry = 0; retry < GOODIX_BERLIN_CMD_RETRY; retry++) {
\t\terror = regmap_raw_write(cd->regmap, cd->cmd_addr, buf,
\t\t\t\t\t sizeof(buf));
\t\tif (error)
\t\t\treturn error;

\t\tfor (i = 0; i < GOODIX_BERLIN_CMD_RETRY; i++) {
\t\t\terror = regmap_raw_read(cd->regmap, cd->cmd_addr, ack,
\t\t\t\t\t\tsizeof(ack));
\t\t\tif (error)
\t\t\t\treturn error;
\t\t\tif (ack[1] == GOODIX_BERLIN_CMD_ACK_OK)
\t\t\t\treturn 0;
\t\t\tusleep_range(1000, 1100);
\t\t\tif (ack[1] != GOODIX_BERLIN_CMD_ACK_BUSY && ack[1])
\t\t\t\tbreak;
\t\t}
\t}

\tdev_err(cd->dev, "command 0x%02x not acked\\n", cmd);
\treturn -EIO;
}

static bool goodix_berlin_gesture_aod_active(const struct goodix_berlin_core *cd)
{
\treturn (cd->aod_status || cd->fod_icon_status) && !cd->nonui_status;
}

/* Called with dt2w_lock held */
static int goodix_berlin_arm_gesture_locked(struct goodix_berlin_core *cd)
{
\tbool aod = goodix_berlin_gesture_aod_active(cd);
\tu8 data[2];

\tif (cd->dt2w && aod) {
\t\tdata[0] = 0x00;
\t\tdata[1] = 0x00;
\t} else if (cd->dt2w) {
\t\tdata[0] = 0x00;
\t\tdata[1] = 0x10;
\t} else if (aod) {
\t\tdata[0] = 0x80;
\t\tdata[1] = 0x00;
\t} else {
\t\tdata[0] = 0x80;
\t\tdata[1] = 0x10;
\t}

\treturn goodix_berlin_send_cmd(cd, GOODIX_BERLIN_CMD_GESTURE,
\t\t\t\t      data, sizeof(data));
}

/* Called with dt2w_lock held */
static int goodix_berlin_send_bool_cmd_locked(struct goodix_berlin_core *cd,
\t\t\t\t\t      u8 cmd, bool on)
{
\tu8 data = on ? 1 : 0;

\tif (cd->screen_off || cd->gesture_mode)
\t\treturn 0;

\treturn goodix_berlin_send_cmd(cd, cmd, &data, sizeof(data));
}

/* Called with dt2w_lock held */
static int goodix_berlin_send_game_locked(struct goodix_berlin_core *cd)
{
\tbool on = !!cd->touch_mode[Touch_Game_Mode][GET_CUR_VALUE];
\tint up = cd->touch_mode[Touch_UP_THRESHOLD][SET_CUR_VALUE] & 0x7;
\tint tol = cd->touch_mode[Touch_Tolerance][SET_CUR_VALUE] & 0x7;
\tint orient = cd->touch_mode[Touch_Panel_Orientation][SET_CUR_VALUE];
\tint aim = cd->touch_mode[Touch_Aim_Sensitivity][SET_CUR_VALUE] & 0x7;
\tint stab = cd->touch_mode[Touch_Tap_Stability][SET_CUR_VALUE] & 0x7;
\tint edge = cd->touch_mode[Touch_Edge_Filter][SET_CUR_VALUE] & 0x3;
\tu8 orient_bits;
\tu8 data[2];

\tif (cd->screen_off || cd->gesture_mode)
\t\treturn 0;

\tif (orient == GOODIX_PANEL_ORIENTATION_90)
\t\torient_bits = 1;
\telse if (orient == GOODIX_PANEL_ORIENTATION_270)
\t\torient_bits = 2;
\telse
\t\torient_bits = 0;

\tdata[0] = (u8)(up | (tol << 3) | (orient_bits << 6));
\tdata[1] = (u8)(stab | (aim << 3) | (edge << 6));

\treturn goodix_berlin_send_cmd(cd,
\t\ton ? GOODIX_BERLIN_CMD_GAME_ON : GOODIX_BERLIN_CMD_GAME_OFF,
\t\tdata, sizeof(data));
}

/* Called with dt2w_lock held */
static void goodix_berlin_reapply_locked(struct goodix_berlin_core *cd)
{
\tif (cd->screen_off || cd->gesture_mode)
\t\treturn;

\tif (cd->charger_on)
\t\tgoodix_berlin_send_bool_cmd_locked(cd, GOODIX_BERLIN_CMD_CHARGER, true);
\tif (cd->palm_status)
\t\tgoodix_berlin_send_bool_cmd_locked(cd, GOODIX_BERLIN_CMD_PALM, true);
\tif (cd->report_rate)
\t\tgoodix_berlin_send_bool_cmd_locked(cd, GOODIX_BERLIN_CMD_REPORT_RATE, true);
\tif (cd->touch_mode[Touch_Game_Mode][GET_CUR_VALUE])
\t\tgoodix_berlin_send_game_locked(cd);
}

/* Called with dt2w_lock held */
static void goodix_berlin_update_gesture_locked(struct goodix_berlin_core *cd)
{
\tbool aod = goodix_berlin_gesture_aod_active(cd);
\tbool want = (cd->dt2w || aod) && cd->screen_off;

\tif (want == cd->gesture_mode) {
\t\tif (want)
\t\t\tgoodix_berlin_arm_gesture_locked(cd);
\t\treturn;
\t}

\tif (want) {
\t\tif (goodix_berlin_arm_gesture_locked(cd))
\t\t\treturn;
\t\tif (!enable_irq_wake(cd->irq))
\t\t\tcd->irq_wake = true;
\t\tWRITE_ONCE(cd->gesture_mode, true);
\t} else {
\t\tif (cd->irq_wake && !disable_irq_wake(cd->irq))
\t\t\tcd->irq_wake = false;
\t\tWRITE_ONCE(cd->gesture_mode, false);
\t\t/* a reset takes the IC back to normal reporting */
\t\tif (cd->reset_gpio)
\t\t\tgoodix_berlin_request_handle_reset(cd);
\t\tgoodix_berlin_reapply_locked(cd);
\t}
\tdev_info(cd->dev, "gesture mode %s (dt2w=%d aod=%d)\\n",
\t\t cd->gesture_mode ? "on" : "off", cd->dt2w, aod);
}

static void goodix_berlin_blank_work(struct work_struct *work)
{
\tstruct goodix_berlin_core *cd =
\t\tcontainer_of(work, struct goodix_berlin_core, blank_work);
\tbool clear_palm = false;

\tmutex_lock(&cd->dt2w_lock);
\tgoodix_berlin_update_gesture_locked(cd);
\tif (cd->screen_off && READ_ONCE(cd->palm_active)) {
\t\tWRITE_ONCE(cd->palm_active, false);
\t\tclear_palm = true;
\t}
\tmutex_unlock(&cd->dt2w_lock);

\tif (clear_palm)
\t\tupdate_palm_sensor_value(0);
}

static void goodix_berlin_game_work(struct work_struct *work)
{
\tstruct goodix_berlin_core *cd =
\t\tcontainer_of(work, struct goodix_berlin_core, game_work);

\tmutex_lock(&cd->dt2w_lock);
\tgoodix_berlin_send_game_locked(cd);
\tmutex_unlock(&cd->dt2w_lock);
}

static void goodix_berlin_charger_work(struct work_struct *work)
{
\tstruct goodix_berlin_core *cd =
\t\tcontainer_of(work, struct goodix_berlin_core, charger_work);
\tbool supplied = power_supply_is_system_supplied() > 0;

\tmutex_lock(&cd->dt2w_lock);
\tif (supplied != cd->charger_on) {
\t\tcd->charger_on = supplied;
\t\tgoodix_berlin_send_bool_cmd_locked(cd, GOODIX_BERLIN_CMD_CHARGER,
\t\t\t\t\t\t   supplied);
\t}
\tmutex_unlock(&cd->dt2w_lock);
}

static void goodix_berlin_reapply_work(struct work_struct *work)
{
\tstruct goodix_berlin_core *cd =
\t\tcontainer_of(work, struct goodix_berlin_core, reapply_work);

\tmutex_lock(&cd->dt2w_lock);
\tgoodix_berlin_reapply_locked(cd);
\tmutex_unlock(&cd->dt2w_lock);
}

static int goodix_berlin_charger_cb(struct notifier_block *nb,
\t\t\t\t    unsigned long event, void *ptr)
{
\tstruct goodix_berlin_core *cd =
\t\tcontainer_of(nb, struct goodix_berlin_core, charger_notifier);

\tschedule_work(&cd->charger_work);
\treturn NOTIFY_OK;
}

static void goodix_berlin_panel_event(enum panel_event_notifier_tag tag,
\t\t\t\t      struct panel_event_notification *event,
\t\t\t\t      void *data)
{
\tstruct goodix_berlin_core *cd = data;
\tbool off;

\tif (!event || event->notif_data.early_trigger)
\t\treturn;

\tswitch (event->notif_type) {
\tcase DRM_PANEL_EVENT_BLANK:
\tcase DRM_PANEL_EVENT_BLANK_LP:
\t\toff = true;
\t\tbreak;
\tcase DRM_PANEL_EVENT_UNBLANK:
\t\toff = false;
\t\tbreak;
\tdefault:
\t\treturn;
\t}

\tWRITE_ONCE(cd->screen_off, off);
\tschedule_work(&cd->blank_work);
}

static struct goodix_berlin_core *goodix_berlin_xm_cd;

static void goodix_berlin_init_touchmode(struct goodix_berlin_core *cd)
{
\tstruct {
\t\tint mode;
\t\tint min_val;
\t\tint max_val;
\t\tint def_val;
\t\tint cur_val;
\t} init_table[] = {
\t\t{ Touch_Game_Mode,         0, 1, 0, 0 },
\t\t{ Touch_Active_MODE,       0, 1, 0, 0 },
\t\t{ Touch_UP_THRESHOLD,      0, 4, 2, 0 },
\t\t{ Touch_Tolerance,         0, 4, 2, 0 },
\t\t{ Touch_Aim_Sensitivity,   0, 4, 0, 0 },
\t\t{ Touch_Tap_Stability,     0, 4, 0, 0 },
\t\t{ Touch_Edge_Filter,       0, 3, 2, 2 },
\t\t{ Touch_Panel_Orientation, 0, 3, 0, 0 },
\t\t{ Touch_Report_Rate,       0, 1, 0, 0 },
\t\t{ Touch_Aod_Enable,        0, 1, 0, 0 },
\t\t{ Touch_Doubletap_Mode,    0, 1, 0, 0 },
\t\t{ Touch_FodIcon_Enable,    0, 1, 0, 0 },
\t\t{ Touch_Nonui_Mode,        0, 1, 0, 0 },
\t};
\tsize_t i;

\tfor (i = 0; i < ARRAY_SIZE(init_table); i++) {
\t\tint m = init_table[i].mode;

\t\tcd->touch_mode[m][GET_MIN_VALUE] = init_table[i].min_val;
\t\tcd->touch_mode[m][GET_MAX_VALUE] = init_table[i].max_val;
\t\tcd->touch_mode[m][GET_DEF_VALUE] = init_table[i].def_val;
\t\tcd->touch_mode[m][SET_CUR_VALUE] = init_table[i].cur_val;
\t\tcd->touch_mode[m][GET_CUR_VALUE] = init_table[i].cur_val;
\t}
}

static void goodix_berlin_apply_report_rate(struct goodix_berlin_core *cd, bool on)
{
\tmutex_lock(&cd->dt2w_lock);
\tcd->report_rate = on;
\tcd->touch_mode[Touch_Report_Rate][SET_CUR_VALUE] = on ? 1 : 0;
\tcd->touch_mode[Touch_Report_Rate][GET_CUR_VALUE] = on ? 1 : 0;
\tgoodix_berlin_send_bool_cmd_locked(cd, GOODIX_BERLIN_CMD_REPORT_RATE, on);
\tmutex_unlock(&cd->dt2w_lock);
}

static int goodix_berlin_xm_set_mode(int mode, int value)
{
\tstruct goodix_berlin_core *cd = goodix_berlin_xm_cd;
\tint clamped;

\tif (!cd || mode < 0 || mode >= Touch_Mode_NUM)
\t\treturn -EINVAL;

\tif (mode == Touch_Doubletap_Mode || mode == Touch_Aod_Enable ||
\t    mode == Touch_FodIcon_Enable || mode == Touch_Nonui_Mode) {
\t\tbool on = value > 0;

\t\tmutex_lock(&cd->dt2w_lock);
\t\tcd->touch_mode[mode][SET_CUR_VALUE] = on ? 1 : 0;
\t\tcd->touch_mode[mode][GET_CUR_VALUE] = on ? 1 : 0;
\t\tif (mode == Touch_Doubletap_Mode)
\t\t\tcd->dt2w = on;
\t\telse if (mode == Touch_Aod_Enable)
\t\t\tcd->aod_status = on;
\t\telse if (mode == Touch_FodIcon_Enable)
\t\t\tcd->fod_icon_status = on;
\t\telse
\t\t\tcd->nonui_status = on;
\t\tgoodix_berlin_update_gesture_locked(cd);
\t\tmutex_unlock(&cd->dt2w_lock);
\t\treturn 0;
\t}

\tif (mode == Touch_Report_Rate) {
\t\tgoodix_berlin_apply_report_rate(cd, value > 0);
\t\treturn 0;
\t}

\tclamped = clamp(value,
\t\t\tcd->touch_mode[mode][GET_MIN_VALUE],
\t\t\tcd->touch_mode[mode][GET_MAX_VALUE]);
\tmutex_lock(&cd->dt2w_lock);
\tcd->touch_mode[mode][SET_CUR_VALUE] = clamped;
\tcd->touch_mode[mode][GET_CUR_VALUE] = clamped;
\tmutex_unlock(&cd->dt2w_lock);

\tif (mode <= Touch_Panel_Orientation)
\t\tschedule_work(&cd->game_work);

\treturn 0;
}

static int goodix_berlin_xm_get_mode_value(int mode, int value_type)
{
\tstruct goodix_berlin_core *cd = goodix_berlin_xm_cd;

\tif (!cd || mode < 0 || mode >= Touch_Mode_NUM ||
\t    value_type < 0 || value_type >= VALUE_TYPE_SIZE)
\t\treturn -1;

\treturn READ_ONCE(cd->touch_mode[mode][value_type]);
}

static int goodix_berlin_xm_get_mode_all(int mode, int *value)
{
\tstruct goodix_berlin_core *cd = goodix_berlin_xm_cd;

\tif (!cd || !value || mode < 0 || mode >= Touch_Mode_NUM)
\t\treturn -EINVAL;

\tvalue[0] = cd->touch_mode[mode][GET_CUR_VALUE];
\tvalue[1] = cd->touch_mode[mode][GET_DEF_VALUE];
\tvalue[2] = cd->touch_mode[mode][GET_MIN_VALUE];
\tvalue[3] = cd->touch_mode[mode][GET_MAX_VALUE];
\treturn 0;
}

static int goodix_berlin_xm_reset_mode(int mode)
{
\tstruct goodix_berlin_core *cd = goodix_berlin_xm_cd;
\tint i;

\tif (!cd || mode < 0 || mode >= Touch_Mode_NUM)
\t\treturn -EINVAL;

\tif (mode == Touch_Report_Rate) {
\t\tgoodix_berlin_apply_report_rate(cd, false);
\t\treturn 0;
\t}

\tmutex_lock(&cd->dt2w_lock);
\tif (mode == 0) {
\t\tfor (i = 0; i <= Touch_Panel_Orientation; i++) {
\t\t\tif (i == Touch_Panel_Orientation)
\t\t\t\tcontinue;
\t\t\tcd->touch_mode[i][SET_CUR_VALUE] =
\t\t\t\tcd->touch_mode[i][GET_DEF_VALUE];
\t\t\tcd->touch_mode[i][GET_CUR_VALUE] =
\t\t\t\tcd->touch_mode[i][GET_DEF_VALUE];
\t\t}
\t\tschedule_work(&cd->game_work);
\t} else {
\t\tint def = cd->touch_mode[mode][GET_DEF_VALUE];

\t\tcd->touch_mode[mode][SET_CUR_VALUE] = def;
\t\tcd->touch_mode[mode][GET_CUR_VALUE] = def;
\t\tif (mode == Touch_Doubletap_Mode) {
\t\t\tcd->dt2w = !!def;
\t\t\tgoodix_berlin_update_gesture_locked(cd);
\t\t} else if (mode <= Touch_Panel_Orientation) {
\t\t\tschedule_work(&cd->game_work);
\t\t}
\t}
\tmutex_unlock(&cd->dt2w_lock);
\treturn 0;
}

static int goodix_berlin_xm_palm_read(void)
{
\tstruct goodix_berlin_core *cd = goodix_berlin_xm_cd;

\treturn (cd && READ_ONCE(cd->palm_active)) ? 1 : 0;
}

static int goodix_berlin_xm_palm_write(int on)
{
\tstruct goodix_berlin_core *cd = goodix_berlin_xm_cd;
\tbool enable = !!on;
\tbool clear_palm = false;
\tint ret = 0;

\tif (!cd)
\t\treturn -ENODEV;

\tmutex_lock(&cd->dt2w_lock);
\tWRITE_ONCE(cd->palm_status, enable);
\tif (!enable && READ_ONCE(cd->palm_active)) {
\t\tWRITE_ONCE(cd->palm_active, false);
\t\tclear_palm = true;
\t}
\tret = goodix_berlin_send_bool_cmd_locked(cd, GOODIX_BERLIN_CMD_PALM, enable);
\tmutex_unlock(&cd->dt2w_lock);

\tif (clear_palm)
\t\tupdate_palm_sensor_value(0);

\treturn ret;
}

static char goodix_berlin_xm_vendor_read(void)
{
\treturn '2';
}

/*
 * The panel registers once the display driver probes, and xiaomi_touch may
 * finish probing after us too, so keep looking for both for a while.
 */
static void goodix_berlin_panel_work(struct work_struct *work)
{
\tstruct goodix_berlin_core *cd = container_of(to_delayed_work(work),
\t\t\t\t\tstruct goodix_berlin_core, panel_work);
\tstruct device_node *np = NULL;
\tstruct drm_panel *panel;

\tif (!cd->panel_cookie) {
\t\twhile ((np = of_find_node_with_property(np,
\t\t\t\t\t"qcom,mdss-dsi-panel-name"))) {
\t\t\tpanel = of_drm_find_panel(np);
\t\t\tif (IS_ERR(panel))
\t\t\t\tcontinue;
\t\t\tcd->panel_cookie = panel_event_notifier_register(
\t\t\t\t\tPANEL_EVENT_NOTIFICATION_PRIMARY,
\t\t\t\t\tPANEL_EVENT_NOTIFIER_CLIENT_PRIMARY_TOUCH,
\t\t\t\t\tpanel, goodix_berlin_panel_event, cd);
\t\t\tif (IS_ERR(cd->panel_cookie))
\t\t\t\tcd->panel_cookie = NULL;
\t\t\tof_node_put(np);
\t\t\tbreak;
\t\t}
\t}

\tif (!goodix_berlin_xm_cd && get_xiaomi_touch_dev()) {
\t\tstruct xiaomi_touch_interface *xm;

\t\txm = devm_kzalloc(cd->dev, sizeof(*xm), GFP_KERNEL);
\t\tif (xm) {
\t\t\tgoodix_berlin_xm_cd = cd;
\t\t\txm->setModeValue = goodix_berlin_xm_set_mode;
\t\t\txm->getModeValue = goodix_berlin_xm_get_mode_value;
\t\t\txm->getModeAll = goodix_berlin_xm_get_mode_all;
\t\t\txm->resetMode = goodix_berlin_xm_reset_mode;
\t\t\txm->palm_sensor_read = goodix_berlin_xm_palm_read;
\t\t\txm->palm_sensor_write = goodix_berlin_xm_palm_write;
\t\t\txm->touch_vendor_read = goodix_berlin_xm_vendor_read;
\t\t\txiaomitouch_register_modedata(0, xm);
\t\t}
\t}

\tif ((!cd->panel_cookie || !goodix_berlin_xm_cd) &&
\t    ++cd->panel_tries < 120)
\t\tschedule_delayed_work(&cd->panel_work, HZ);
\telse
\t\tdev_info(cd->dev, "xiaomi touch: panel %s, xiaomi_touch %s\\n",
\t\t\t cd->panel_cookie ? "found" : "missing",
\t\t\t goodix_berlin_xm_cd ? "found" : "missing");
}

static void goodix_berlin_gesture_irq(struct goodix_berlin_core *cd)
{
\tunsigned int key = 0;

\tif (cd->event.hdr.status & GOODIX_BERLIN_GESTURE_EVENT) {
\t\tu8 gtype = cd->event.hdr.reserved2[1];

\t\tif (READ_ONCE(cd->dt2w) &&
\t\t    gtype == GOODIX_BERLIN_GESTURE_DOUBLE_TAP)
\t\t\tkey = KEY_WAKEUP;
\t\telse if (goodix_berlin_gesture_aod_active(cd) &&
\t\t\t gtype == GOODIX_BERLIN_GESTURE_SINGLE_TAP)
\t\t\tkey = KEY_GOTO;
\t}

\tif (key) {
\t\tinput_report_key(cd->input_dev, key, 1);
\t\tinput_sync(cd->input_dev);
\t\tinput_report_key(cd->input_dev, key, 0);
\t\tinput_sync(cd->input_dev);
\t}

\tregmap_write(cd->regmap, cd->touch_data_addr, 0);
\tmutex_lock(&cd->dt2w_lock);
\tif (cd->gesture_mode)
\t\tgoodix_berlin_arm_gesture_locked(cd);
\tmutex_unlock(&cd->dt2w_lock);
}

static irqreturn_t goodix_berlin_irq(int irq, void *data)
{""",
)

sub(
    """\tif (cd->event.hdr.status & GOODIX_BERLIN_TOUCH_EVENT)
\t\tgoodix_berlin_touch_handler(cd);

\tif (cd->event.hdr.status & GOODIX_BERLIN_REQUEST_EVENT) {
\t\tswitch (cd->event.hdr.request_type) {
\t\tcase GOODIX_BERLIN_REQUEST_CODE_RESET:
\t\t\tif (cd->reset_gpio)
\t\t\t\tgoodix_berlin_request_handle_reset(cd);
\t\t\tbreak;""",
    """\tif (READ_ONCE(cd->gesture_mode)) {
\t\tgoodix_berlin_gesture_irq(cd);
\t\tgoto out;
\t}

\tif (cd->event.hdr.status & GOODIX_BERLIN_TOUCH_EVENT) {
\t\tif (READ_ONCE(cd->palm_status)) {
\t\t\tif (cd->event.hdr.request_type & GOODIX_BERLIN_LARGE_TOUCH_EVENT) {
\t\t\t\tif (!READ_ONCE(cd->palm_active)) {
\t\t\t\t\tWRITE_ONCE(cd->palm_active, true);
\t\t\t\t\tupdate_palm_sensor_value(1);
\t\t\t\t}
\t\t\t\tgoto out_clear;
\t\t\t} else if (READ_ONCE(cd->palm_active)) {
\t\t\t\tWRITE_ONCE(cd->palm_active, false);
\t\t\t\tupdate_palm_sensor_value(0);
\t\t\t}
\t\t} else if (READ_ONCE(cd->palm_active)) {
\t\t\tWRITE_ONCE(cd->palm_active, false);
\t\t\tupdate_palm_sensor_value(0);
\t\t}
\t\tgoodix_berlin_touch_handler(cd);
\t}

\tif (cd->event.hdr.status & GOODIX_BERLIN_REQUEST_EVENT) {
\t\tswitch (cd->event.hdr.request_type) {
\t\tcase GOODIX_BERLIN_REQUEST_CODE_RESET:
\t\t\tif (cd->reset_gpio) {
\t\t\t\tgoodix_berlin_request_handle_reset(cd);
\t\t\t\tschedule_work(&cd->reapply_work);
\t\t\t}
\t\t\tbreak;""",
)

sub(
    """\tinput_set_abs_params(cd->input_dev, ABS_MT_TOUCH_MAJOR, 0, 255, 0, 0);
""",
    """\tinput_set_abs_params(cd->input_dev, ABS_MT_TOUCH_MAJOR, 0, 255, 0, 0);
\tinput_set_capability(cd->input_dev, EV_KEY, KEY_WAKEUP);
\tinput_set_capability(cd->input_dev, EV_KEY, KEY_GOTO);
""",
)

sub(
    """\tstruct goodix_berlin_core *cd = dev_get_drvdata(dev);

\tdisable_irq(cd->irq);
\tgoodix_berlin_power_off(cd);

\treturn 0;
}""",
    """\tstruct goodix_berlin_core *cd = dev_get_drvdata(dev);

\tdisable_irq(cd->irq);
\tif (READ_ONCE(cd->gesture_mode))
\t\treturn 0;
\tgoodix_berlin_power_off(cd);

\treturn 0;
}""",
)

sub(
    """\tstruct goodix_berlin_core *cd = dev_get_drvdata(dev);
\tint error;

\terror = goodix_berlin_power_on(cd);
\tif (error)
\t\treturn error;

\tenable_irq(cd->irq);""",
    """\tstruct goodix_berlin_core *cd = dev_get_drvdata(dev);
\tint error;

\tif (READ_ONCE(cd->gesture_mode)) {
\t\tenable_irq(cd->irq);
\t\treturn 0;
\t}

\terror = goodix_berlin_power_on(cd);
\tif (error)
\t\treturn error;

\tenable_irq(cd->irq);
\tschedule_work(&cd->reapply_work);""",
)

sub(
    """static const BIN_ATTR_ADMIN_RW(registers, 0);

static const struct bin_attribute *const goodix_berlin_bin_attrs[] = {
\t&bin_attr_registers,
\tNULL,
};

static const struct attribute_group goodix_berlin_attr_group = {
\t.bin_attrs = goodix_berlin_bin_attrs,
};""",
    """static ssize_t switch_report_rate_show(struct device *dev,
\t\t\t\t       struct device_attribute *attr, char *buf)
{
\tstruct goodix_berlin_core *cd = dev_get_drvdata(dev);

\treturn sysfs_emit(buf, "%s\\n",
\t\t\tREAD_ONCE(cd->report_rate) ? "480HZ" : "240HZ");
}

static ssize_t switch_report_rate_store(struct device *dev,
\t\t\t\t\tstruct device_attribute *attr,
\t\t\t\t\tconst char *buf, size_t count)
{
\tstruct goodix_berlin_core *cd = dev_get_drvdata(dev);
\tbool on;

\tif (kstrtobool(buf, &on))
\t\treturn -EINVAL;

\tgoodix_berlin_apply_report_rate(cd, on);
\treturn count;
}

static DEVICE_ATTR_RW(switch_report_rate);

static ssize_t game_mode_show(struct device *dev,
\t\t\t      struct device_attribute *attr, char *buf)
{
\tstruct goodix_berlin_core *cd = dev_get_drvdata(dev);

\treturn sysfs_emit(buf, "%d\\n",
\t\t\t!!READ_ONCE(cd->touch_mode[Touch_Game_Mode][GET_CUR_VALUE]));
}

static ssize_t game_mode_store(struct device *dev,
\t\t\t       struct device_attribute *attr,
\t\t\t       const char *buf, size_t count)
{
\tbool on;
\tint ret;

\tif (kstrtobool(buf, &on))
\t\treturn -EINVAL;

\tret = goodix_berlin_xm_set_mode(Touch_Game_Mode, on);
\treturn ret ? ret : count;
}

static DEVICE_ATTR_RW(game_mode);

static struct attribute *goodix_berlin_attrs[] = {
\t&dev_attr_switch_report_rate.attr,
\t&dev_attr_game_mode.attr,
\tNULL,
};

static const BIN_ATTR_ADMIN_RW(registers, 0);

static const struct bin_attribute *const goodix_berlin_bin_attrs[] = {
\t&bin_attr_registers,
\tNULL,
};

static const struct attribute_group goodix_berlin_attr_group = {
\t.attrs = goodix_berlin_attrs,
\t.bin_attrs = goodix_berlin_bin_attrs,
};""",
)

sub(
    """static void goodix_berlin_power_off_act(void *data)
{
\tstruct goodix_berlin_core *cd = data;

\tgoodix_berlin_power_off(cd);
}""",
    """static void goodix_berlin_power_off_act(void *data)
{
\tstruct goodix_berlin_core *cd = data;

\tgoodix_berlin_power_off(cd);
}

static void goodix_berlin_cleanup_act(void *data)
{
\tstruct goodix_berlin_core *cd = data;

\tpower_supply_unreg_notifier(&cd->charger_notifier);
\tcancel_delayed_work_sync(&cd->panel_work);
\tcancel_work_sync(&cd->blank_work);
\tcancel_work_sync(&cd->game_work);
\tcancel_work_sync(&cd->charger_work);
\tcancel_work_sync(&cd->reapply_work);
\tif (cd->panel_cookie)
\t\tpanel_event_notifier_unregister(cd->panel_cookie);
\tif (goodix_berlin_xm_cd == cd)
\t\tgoodix_berlin_xm_cd = NULL;
}""",
)

sub(
    """\tdev_set_drvdata(dev, cd);

\tdev_dbg(dev, "Goodix Berlin %s Touchscreen Controller",""",
    """\tdev_set_drvdata(dev, cd);

\tmutex_init(&cd->dt2w_lock);
\tgoodix_berlin_init_touchmode(cd);
\tINIT_WORK(&cd->blank_work, goodix_berlin_blank_work);
\tINIT_WORK(&cd->game_work, goodix_berlin_game_work);
\tINIT_WORK(&cd->charger_work, goodix_berlin_charger_work);
\tINIT_WORK(&cd->reapply_work, goodix_berlin_reapply_work);
\tINIT_DELAYED_WORK(&cd->panel_work, goodix_berlin_panel_work);
\tcd->charger_notifier.notifier_call = goodix_berlin_charger_cb;
\tpower_supply_reg_notifier(&cd->charger_notifier);
\terror = devm_add_action_or_reset(dev, goodix_berlin_cleanup_act, cd);
\tif (error)
\t\treturn error;
\tschedule_delayed_work(&cd->panel_work, 0);
\tschedule_work(&cd->charger_work);

\tdev_dbg(dev, "Goodix Berlin %s Touchscreen Controller",""",
)

with open(core_path, "w", encoding="utf-8", newline="\n") as f:
    f.write(src)

print("xm_touch: OK")
