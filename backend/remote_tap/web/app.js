"use strict";

/**
 * RemoteTap dashboard.
 *
 * Talks to the local bridge over a WebSocket. The bridge — not this page —
 * holds the TCP connection to the device, owns the press lease and enforces
 * nothing here. This script only:
 *
 *   - renders the state the bridge streams, and
 *   - turns pointer events into "press_start" / "press_stop" requests.
 *
 * It never decides how long a press may last. The device does that.
 *
 * On pointer down it sends press_start; on pointer up, pointer cancellation,
 * lost pointer capture, the window losing focus, or the page becoming hidden it
 * sends press_stop. Those later cases are a convenience so the button does not
 * appear stuck: even if none of them fired, the device would still release the
 * press when the lease lapsed.
 */
(function () {
  var els = {
    deviceValue: document.getElementById("device-value"),
    deviceHint: document.getElementById("device-hint"),
    chipDevice: document.getElementById("chip-device"),
    actuatorValue: document.getElementById("actuator-value"),
    actuatorHint: document.getElementById("actuator-hint"),
    chipActuator: document.getElementById("chip-actuator"),
    heartbeatValue: document.getElementById("heartbeat-value"),
    heartbeatHint: document.getElementById("heartbeat-hint"),
    chipHeartbeat: document.getElementById("chip-heartbeat"),
    power: document.getElementById("power"),
    powerCaption: document.getElementById("power-caption"),
    controlHint: document.getElementById("control-hint"),
    detailHeartbeat: document.getElementById("detail-heartbeat"),
    detailSequence: document.getElementById("detail-sequence"),
    detailSession: document.getElementById("detail-session"),
    detailLease: document.getElementById("detail-lease"),
    notice: document.getElementById("notice"),
  };

  var TRANSIENT_MS = 1200;

  var latest = null;
  var transient = null;
  var transientTimer = null;
  var pressing = false;
  var socket = null;
  var reconnectDelay = 500;

  // ---------------------------------------------------------------- socket

  function websocketUrl() {
    var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    return scheme + "//" + window.location.host + "/ws";
  }

  function connect() {
    socket = new WebSocket(websocketUrl());

    socket.addEventListener("open", function () {
      reconnectDelay = 500;
      showNotice(null);
    });

    socket.addEventListener("message", function (event) {
      var message;
      try {
        message = JSON.parse(event.data);
      } catch (error) {
        return;
      }
      if (message.type === "state") {
        render(message);
      } else if (message.type === "notice") {
        onNotice(message);
      }
    });

    socket.addEventListener("close", onSocketClosed);
    socket.addEventListener("error", function () {
      if (socket) {
        socket.close();
      }
    });
  }

  function onSocketClosed() {
    socket = null;
    // The bridge releases the press on our behalf when the socket closes, so
    // the local "held" styling must not linger.
    pressing = false;
    els.power.classList.remove("is-held");
    els.power.setAttribute("aria-pressed", "false");
    renderDisconnected();
    window.setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 5000);
  }

  function send(payload) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
    }
  }

  // -------------------------------------------------------------- rendering

  function render(snapshot) {
    latest = snapshot;
    var connected = !!(snapshot.device && snapshot.device.connected);
    var active = !!(snapshot.press && snapshot.press.active);
    var udp = snapshot.udp || {};
    var udpOnline = !!udp.online;

    setChip(
      els.chipDevice,
      els.deviceValue,
      els.deviceHint,
      connected ? "Connected" : "Disconnected",
      connected ? "ok" : "error",
      snapshot.device ? snapshot.device.host + ":" + snapshot.device.port : "no address"
    );

    setChip(
      els.chipHeartbeat,
      els.heartbeatValue,
      els.heartbeatHint,
      udpOnline ? "ONLINE" : "OFFLINE",
      udpOnline ? "ok" : "warn",
      heartbeatHint(udp)
    );

    var actuator = !connected ? "ERROR" : active ? "PRESSING" : "IDLE";
    if (transient) {
      actuator = transient;
    }
    setChip(
      els.chipActuator,
      els.actuatorValue,
      els.actuatorHint,
      actuator,
      actuator === "ERROR" ? "error" : actuator === "PRESSING" ? "active" : "ok",
      connected ? "simulated" : "no device link"
    );

    els.power.disabled = !connected;
    els.power.classList.toggle("is-releasing", actuator === "RELEASING");
    els.power.classList.toggle("is-error", actuator === "ERROR");
    if (!pressing) {
      els.powerCaption.textContent = connected ? "Hold to press" : "Unavailable";
    }

    els.detailHeartbeat.textContent = formatClock(udp.last_heartbeat_ms);
    els.detailSequence.textContent = valueOrDash(udp.sequence);
    els.detailSession.textContent = shortId(snapshot.press && snapshot.press.session_id);
    els.detailLease.textContent = leaseText(snapshot.press);
  }

  function renderDisconnected() {
    setChip(els.chipDevice, els.deviceValue, els.deviceHint, "Rebuilding link", "warn", "\u2014");
    els.power.disabled = true;
    els.powerCaption.textContent = "Unavailable";
  }

  function setChip(chip, valueEl, hintEl, value, tone, hint) {
    valueEl.textContent = value;
    hintEl.textContent = hint;
    chip.classList.toggle("is-ok", tone === "ok");
    chip.classList.toggle("is-warn", tone === "warn");
    chip.classList.toggle("is-error", tone === "error");
    chip.classList.toggle("is-active", tone === "active");
  }

  function heartbeatHint(udp) {
    if (!udp.listening) {
      return "not listening";
    }
    if (udp.age_ms === null || udp.age_ms === undefined) {
      return "no heartbeat yet";
    }
    return udp.age_ms + " ms ago";
  }

  function leaseText(press) {
    if (!press || press.renew_interval_ms == null) {
      return "\u2014";
    }
    return press.renew_interval_ms + " / " + press.max_press_ms + " ms";
  }

  function formatClock(ms) {
    if (ms === null || ms === undefined) {
      return "\u2014";
    }
    var date = new Date(ms);
    return date.toLocaleTimeString();
  }

  function shortId(id) {
    if (!id) {
      return "\u2014";
    }
    return id.slice(0, 8) + "\u2026";
  }

  function valueOrDash(value) {
    return value === null || value === undefined ? "\u2014" : String(value);
  }

  function onNotice(message) {
    showNotice(message.level === "error" ? message.message : null);
    if (message.level === "error") {
      markTransient("ERROR");
    }
  }

  function showNotice(text) {
    if (!text) {
      els.notice.hidden = true;
      els.notice.textContent = "";
      return;
    }
    els.notice.textContent = text;
    els.notice.dataset.level = "error";
    els.notice.hidden = false;
  }

  function markTransient(label) {
    transient = label;
    if (transientTimer !== null) {
      window.clearTimeout(transientTimer);
    }
    transientTimer = window.setTimeout(function () {
      transient = null;
      transientTimer = null;
      if (latest) {
        render(latest);
      }
    }, TRANSIENT_MS);
    if (latest) {
      render(latest);
    }
  }

  // -------------------------------------------------------- press and hold

  function onPointerDown(event) {
    if (typeof event.button === "number" && event.button !== 0) {
      return; // primary button / first touch only
    }
    if (pressing || els.power.disabled) {
      return;
    }
    if (!latest || !latest.device || !latest.device.connected) {
      return;
    }
    event.preventDefault();
    pressing = true;
    els.power.classList.add("is-held");
    els.power.classList.remove("is-releasing", "is-error");
    els.power.setAttribute("aria-pressed", "true");
    els.powerCaption.textContent = "Holding";
    if (els.power.setPointerCapture) {
      try {
        els.power.setPointerCapture(event.pointerId);
      } catch (error) {
        /* capture is best effort */
      }
    }
    send({ action: "press_start" });
  }

  function releaseHold() {
    if (!pressing) {
      return;
    }
    pressing = false;
    els.power.classList.remove("is-held");
    els.power.setAttribute("aria-pressed", "false");
    els.powerCaption.textContent = "Hold to press";
    markTransient("RELEASING");
    send({ action: "press_stop" });
  }

  els.power.addEventListener("pointerdown", onPointerDown);
  els.power.addEventListener("pointerup", releaseHold);
  els.power.addEventListener("pointercancel", releaseHold);
  els.power.addEventListener("lostpointercapture", releaseHold);
  els.power.addEventListener("contextmenu", function (event) {
    // A long press on a touchscreen must not open the context menu.
    event.preventDefault();
  });
  els.power.addEventListener("dragstart", function (event) {
    event.preventDefault();
  });

  window.addEventListener("blur", releaseHold);
  window.addEventListener("pagehide", releaseHold);
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      releaseHold();
    }
  });

  renderDisconnected();
  connect();
})();
