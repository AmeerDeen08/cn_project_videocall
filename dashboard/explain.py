"""Turn live stats into a plain-language explanation of what the call is doing."""

MODE_NAMES = {"off": "No FEC", "fixed": "Fixed FEC", "adaptive": "Adaptive FEC"}


def _fmt(v, nd=0):
    return f"{v:.{nd}f}"


def _trend(history, key, now, threshold):
    """'rose' / 'fell' / 'steady', comparing now with ~5 seconds ago."""
    past = [h[key] for h in history[-12:-8] if h.get(key) is not None]
    if not past or now is None:
        return "steady"
    delta = now - past[-1]
    if delta > threshold:
        return "rose"
    if delta < -threshold:
        return "fell"
    return "steady"


def explain(snap):
    """Return {level, title, body} where level is idle | ok | warn | bad."""
    rx = snap["rx"]
    if not rx["active"]:
        return {
            "level": "idle",
            "title": "Waiting for video",
            "body": f"No frames from {snap['config']['peer']} in the last few "
                    "seconds. Check that the other peer is running and that the "
                    "ports match.",
        }

    mode = rx["mode"]
    loss = rx["loss_pct"] or 0.0
    rec = rx["recovery_pct"] or 0.0
    rescued = rx["rescued_pct"] or 0.0
    k, r = rx["k"], rx["r"]
    overhead = rx["overhead_pct"] or 0.0
    budget = rx["budget_pct"] or 0.0
    history = snap.get("history", [])

    loss_trend = _trend(history, "loss", loss, 3)
    loss_phrase = {"rose": f"Loss rose to {_fmt(loss)}%",
                   "fell": f"Loss fell to {_fmt(loss)}%",
                   "steady": f"Loss is {_fmt(loss)}%"}[loss_trend]

    if mode == "off":
        if rec >= 99:
            return {
                "level": "ok",
                "title": "Clean link, no FEC needed right now",
                "body": f"{loss_phrase}. FEC is off, so each frame needs all {k} "
                        "of its packets to arrive — which they are.",
            }
        return {
            "level": "bad",
            "title": "Video is breaking up",
            "body": f"{loss_phrase}. FEC is off, so a frame survives only if all "
                    f"{k} of its packets arrive — just {_fmt(rec)}% did in the "
                    "last 3 s. Missing packets show up as corrupted blocks and "
                    "freezes, and there is no time to retransmit them.",
        }

    if mode == "adaptive":
        over_trend = _trend(history, "overhead", overhead, 4)
        action = {"rose": "increased redundancy to",
                  "fell": "reduced redundancy to",
                  "steady": "is holding redundancy at"}[over_trend]
        fec_phrase = f"Adaptive FEC {action} {_fmt(overhead)}%"
    else:
        fec_phrase = f"Fixed FEC adds {r} parity packets per frame ({_fmt(overhead)}%)"

    if rec >= 99:
        if loss < 0.5:
            tail = ("so there is little to repair and almost no bandwidth is wasted."
                    if mode == "adaptive" else
                    "as insurance; nothing needs repairing right now.")
            return {"level": "ok", "title": "Clean link, call stable",
                    "body": f"{loss_phrase}. {fec_phrase} {tail}"}
        return {
            "level": "ok",
            "title": "Call stable — FEC is rebuilding lost packets",
            "body": f"{loss_phrase}. {fec_phrase} to compensate — {_fmt(rescued)}% "
                    "of frames lost packets and every one was fully recovered "
                    "without retransmission. Call stable.",
        }

    unrecovered = 100 - rec
    if rec >= 90:
        hint = (" Adaptive FEC is raising redundancy to catch up."
                if mode == "adaptive" else
                " Fixed redundancy cannot grow — adaptive mode would add more parity.")
        return {
            "level": "warn",
            "title": "At the FEC budget",
            "body": f"{loss_phrase}, close to what {r} parity packets can absorb "
                    f"({_fmt(budget)}% of each frame). {_fmt(unrecovered)}% of "
                    f"frames could not be rebuilt.{hint}",
        }
    hint = (" Adaptive FEC is ramping up redundancy." if mode == "adaptive" else
            " Fixed redundancy cannot grow to match it.")
    return {
        "level": "bad",
        "title": "Loss exceeds the FEC budget",
        "body": f"{loss_phrase}, but {r} parity packets only cover "
                f"{_fmt(budget)}% of each frame. Only {_fmt(rec)}% of frames were "
                f"recovered; the rest show as corrupted or frozen.{hint}",
    }
