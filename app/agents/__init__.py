"""The supervisor and the four agents a customer turn is delegated to.

Sara shops, Nora designs, Salman sells and Faysal supports. Each is its own
model with its own tools, and they share one memory: the session state this
turn stages and the record of what every tool did. Facts come only from the
services behind the tools; store scope comes only from the request.
"""
