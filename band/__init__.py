"""Band — the governed coordination room (deliberation + authority gate + audit).

Sponsor boundary: Band is where agents reconcile *under verified authority* and
produce an audit trail. The pipeline talks to a ``BandRoomBackend`` (see
``band.adapter``); ``LocalBandRoom`` runs the governance in-process today, and
``RemoteBandRoom`` is where a real Band service/SDK plugs in.
"""
