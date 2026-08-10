"""`net` — core module. The single chokepoint for every HTTP request PoEDex makes.

No other module may open a socket. One limiter has to see every request or the
account gets restricted (IMPLEMENTATION-PLAN §4).
"""
