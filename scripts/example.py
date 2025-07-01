def init(send_response_fn, config):
    """
    Called once by PluginManager.
     - send_response_fn: call this to send a dict back to the UI.
     - config: your own YAML dict (hostname, attached_name, etc).
    """
    global _send, _cfg
    _send, _cfg = send_response_fn, config

def handle_message(msg):
    """
    Called whenever the discovery app gets a "record" (or any) command
    *and* the user has that device checkbox ticked.
    
    'msg' is the JSON dict your main controller posted.
    You can:
      - speak OSC to the iPhone
      - call Shogun's REST API
      - do any other side effects
    When you have progress or a status (e.g. 'started', 'error'), call:
        _send({'type':'status','msg':'recording started'})
    """
    # # example: send OSC
    # from pythonosc import udp_client
    # client = udp_client.SimpleUDPClient(_cfg['ip'], _cfg.get('osc_port', 8080))
    # client.send_message("/liveLinkFace/control", msg['action'])
    # _send({'type':'status','msg':f"sent {msg['action']} to phone"})

    # example just print the message
    # print(f"Received message: {msg}")
    # print(f"Discovered ip: {msg.get('ip')}")
    # print(f"Config: {_cfg}")
    if msg.get('type') == 'health' and msg.get('ip') is not None:
        _send({
            'type':   'health_response',
            'device': _cfg['attached_name'],
            'value':     True
        })
        return
