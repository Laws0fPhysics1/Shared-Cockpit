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
        function instruments() {
            var panel = window.document.getElementById("panel");
            return panel ? panel.children : [];
        }
        // Instrument GUIDs are assigned at runtime and differ between the two PCs, so a
        // targeted event is identified by the instrument's element name instead.
        function replay(msg) {
            try {
                var list = instruments();
                var args = [msg.n].concat(msg.a || []);
                for (var i = 0; i < list.length; i++) {
                    var instrument = list[i];
                    if (!instrument || typeof instrument.onInteractionEvent !== "function") { continue; }
                    if (msg.i && String(instrument.tagName).toLowerCase() !== msg.i) { continue; }
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
                send({ t: "hello", panel: panelName(), v: 2 });
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
                var tag = null;
                if (_target) {
                    // Targeted at one instrument: only the panel holding it reports it.
                    var list = instruments();
                    for (var i = 0; i < list.length; i++) {
                        if (list[i] && list[i].getAttribute && list[i].getAttribute("Guid") == _target) {
                            tag = String(list[i].tagName).toLowerCase();
                            break;
                        }
                    }
                    if (!tag) { return; }
                }
                send({ t: "h", n: String(_args[0]), a: Array.prototype.slice.call(_args, 1), i: tag });
            } catch (e) { }
        });
        connect();
    } catch (e) { }
})();
