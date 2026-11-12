(function () {
    // Shared Cockpit bridge: reports every cockpit interaction (H: event) of this
    // panel to the Shared Cockpit app on this PC, and replays the co-pilot's
    // interactions straight into this panel's instruments (never through
    // Coherent, so a replayed press is not reported again = no echo).
    try {
        if (window.__sharedCockpitHook) { return; }
        window.__sharedCockpitHook = true;
        var URL = "ws://127.0.0.1:__SC_PORT__";
        var sock = null;
        var open = false;
        function panelName() {
            try { return (typeof globalPanelData !== "undefined" && globalPanelData && globalPanelData.sName) || ""; }
            catch (e) { return ""; }
        }
        function send(obj) {
            if (!open || !sock) { return; }
            try { sock.send(JSON.stringify(obj)); } catch (e) { }
        }
        function replay(msg) {
            try {
                var panel = window.document.getElementById("panel");
                if (!panel) { return; }
                var args = [msg.n].concat(msg.a || []);
                for (var i = 0; i < panel.children.length; i++) {
                    var instrument = panel.children[i];
                    if (!instrument || typeof instrument.onInteractionEvent !== "function") { continue; }
                    if (msg.g && instrument.getAttribute("Guid") != msg.g) { continue; }
                    try { instrument.onInteractionEvent(args); } catch (e) { }
                }
            } catch (e) { }
        }
        function connect() {
            try {
                sock = new WebSocket(URL);
            } catch (e) {
                sock = null;
                setTimeout(connect, 2000);
                return;
            }
            sock.onopen = function () {
                open = true;
                send({ t: "hello", panel: panelName(), v: 1 });
            };
            sock.onmessage = function (ev) {
                try {
                    var msg = JSON.parse(ev.data);
                    if (msg && msg.t === "h" && typeof msg.n === "string") { replay(msg); }
                } catch (e) { }
            };
            sock.onclose = function () {
                open = false;
                sock = null;
                setTimeout(connect, 2000);
            };
            sock.onerror = function () {
                try { if (sock) { sock.close(); } } catch (e) { }
            };
        }
        Coherent.on("OnInteractionEvent", function (_target, _args) {
            try {
                if (!_args || !_args.length) { return; }
                send({ t: "h", n: String(_args[0]), a: Array.prototype.slice.call(_args, 1), g: _target || null });
            } catch (e) { }
        });
        connect();
    } catch (e) { }
})();
