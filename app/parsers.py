import pprint
import time
import util
from models.game import Game, Player
from models.set import Zone
import app.mtga_app


def parse_jsonrpc_blob(blob):
    pass


def parse_get_decklists(blob):
    """ CHECK """
    for deck in blob["Deck.GetDeckLists"]:
        util.process_deck(deck)


def parse_event_joinqueue(blob):
    import app.mtga_app as mtga_app
    # method = 'Event.JoinQueue'
    """ CHECK """
    params = blob['params']
    deckId = params['deckId']
    return mtga_app.mtga_watch_app.player_decks[deckId]


def parse_game_state_message(message):
    import app.mtga_app as mtga_app
    with mtga_app.mtga_watch_app.game_lock:  # the game state may become inconsistent in between these steps, so lock it
        if 'annotations' in message.keys():
            for annotation in message['annotations']:
                annotation_type = annotation['type'][0]
                if annotation_type == 'AnnotationType_ObjectIdChanged':
                    try:
                        original_id = None
                        new_id = None
                        details = annotation['details']
                        for detail in details:
                            if detail['key'] == "orig_id":
                                original_id = detail["valueInt32"][0]
                                mtga_app.mtga_watch_app.game.ignored_iids.add(original_id)
                            elif detail['key'] == "new_id":
                                new_id = detail["valueInt32"][0]
                        card_with_iid = mtga_app.mtga_watch_app.game.find_card_by_iid(original_id)
                        if not card_with_iid:  # no one has ref'd yet, we don't care
                            continue
                        new_card_already_exists = mtga_app.mtga_watch_app.game.find_card_by_iid(new_id)
                        if new_card_already_exists:  # just wipe the old card, the new card is already there
                            assert new_card_already_exists.mtga_id == card_with_iid.mtga_id or -1 in [new_card_already_exists.mtga_id, card_with_iid.mtga_id], "{} / {}".format(new_card_already_exists.mtga_id , card_with_iid.mtga_id)
                            card_with_iid.mtga_id = -1
                        else:
                            card_with_iid.game_id = new_id

                        # mtga_logger.info("IGNORING IID {}, NOW {}".format(original_id, new_id))
                    except:
                        raise
                        pass        # 99 , 344
        if 'gameObjects' in message.keys():
            game_objects = message['gameObjects']
            for object in game_objects:
                card_id = object['grpId']
                instance_id = object['instanceId']
                if instance_id in mtga_app.mtga_watch_app.game.ignored_iids:
                    continue
                owner = object['controllerSeatId']
                type = object["type"]
                zone = object['zoneId']
                if type != "GameObjectType_Card":
                    mtga_app.mtga_watch_app.game.ignored_iids.add(instance_id)
                else:
                    player, zone = mtga_app.mtga_watch_app.game.get_owner_zone_tup(zone)
                    if zone:
                        if not player:
                            player = mtga_app.mtga_watch_app.game.hero
                            # if zone is shared, don't care what player we use to put this card into it
                        assert isinstance(player, Player)
                        player.put_instance_id_in_zone(instance_id, owner, zone)
                        zone.match_game_id_to_card(instance_id, card_id)
        if 'zones' in message.keys():
            for zone in message['zones']:
                try:
                    parse_zone(zone)
                except:
                    app.mtga_app.mtga_logger.error("error parsing zone:")
                    app.mtga_app.mtga_logger.error(pprint.pformat(zone))
                    time.sleep(1)
                    raise


def parse_zone(zone_blob):
    import app.mtga_app as mtga_app
    trackable_zones = ["ZoneType_Hand", "ZoneType_Library", "ZoneType_Graveyard", "ZoneType_Exile", "ZoneType_Limbo",
                       "ZoneType_Stack", "ZoneType_Battlefield"]
    zone_type = zone_blob["type"]
    if zone_type not in trackable_zones:
        return
    mtga_app.mtga_watch_app.game.register_zone(zone_blob)  # make sure we will find the zone later
    zone_id = zone_blob["zoneId"]
    player, zone = mtga_app.mtga_watch_app.game.get_owner_zone_tup(zone_id)
    if not zone:
        if "ownerSeatId" in zone_blob:
            owner_seat = zone_blob["ownerSeatId"]
            player = mtga_app.mtga_watch_app.game.get_player_in_seat(owner_seat)
            zone = player.get_zone_by_name(zone_type)
            zone.zone_id = zone_id
    if zone and not player:
        # we don't care if there is no owner (i.e. a shared zone), we just need a player to reference
        player = mtga_app.mtga_watch_app.game.hero
    if 'objectInstanceIds' in zone_blob:
        for instance_id in zone_blob['objectInstanceIds']:
            if instance_id in mtga_app.mtga_watch_app.game.ignored_iids:
                continue
            if "ownerSeatId" not in zone_blob:
                card = mtga_app.mtga_watch_app.game.find_card_by_iid(instance_id)
                owner_seat = card.owner_seat_id
            else:
                owner_seat = zone_blob["ownerSeatId"]
            # TODO: logging
            # mtga_logger.info("adding {} to {}".format(instance_id, zone))
            player.put_instance_id_in_zone(instance_id, owner_seat,  zone)
        cards_to_remove_from_zone = []
        for card in zone.cards:
            if card.game_id not in zone_blob['objectInstanceIds']:
                cards_to_remove_from_zone.append(card)
        for card in cards_to_remove_from_zone:
            # mtga_logger.info("removing {} from {}".format(card, zone))
            zone.cards.remove(card)


def parse_mulligan_response(blob):
    import app.mtga_app as mtga_app
    if blob["mulliganResp"]["decision"] == "MulliganOption_Mulligan":
        player = mtga_app.mtga_watch_app.game.get_player_in_seat(blob["systemSeatId"])
        player.do_mulligan()


def parse_accept_hand(blob):
    import app.mtga_app as mtga_app
    client_message = blob['clientToGreMessage']
    response = client_message['mulliganResp']['decision']
    if response == "MulliganOption_AcceptHand":
        with mtga_app.mtga_watch_app.game_lock:
            mtga_app.mtga_watch_app.game.hero.deck.transfer_cards_to(mtga_app.mtga_watch_app.game.temp["my_mulligan"],
                                                                     mtga_app.mtga_watch_app.game.hero.hand)


def parse_match_complete(blob):
    # MatchGameRoomStateType_MatchCompleted
    pass


def parse_match_playing(blob):
    # MatchGameRoomStateType_Playing
    import app.mtga_app as mtga_app
    temp_players = {
        1: {},
        2: {}
    }
    game_room_info = blob["matchGameRoomStateChangedEvent"]["gameRoomInfo"]
    game_room_players = game_room_info["players"]

    for player in game_room_players:
        temp_players[player["systemSeatId"]]["player_id"] = player["userId"]

    game_room_config = game_room_info["gameRoomConfig"]

    reserved_players = game_room_config["reservedPlayers"]
    for player in reserved_players:
        temp_players[player["systemSeatId"]]["name"] = player["playerName"]

    match_config = game_room_config["matchConfig"]
    if "teams" in match_config:
        teams = match_config["teams"]
        for team in teams:
            players = team["players"]
            for player in players:
                player_seat = player["systemSeatId"]
                temp_players[player_seat]["deck"] = util.card_ids_to_card_list(player["deckCards"])
    for player_idx in [1,2]:
        if "deck" not in temp_players[player_idx]:
            temp_players[player_idx]["deck"] = []
    # set up shared zones
    shared_battlefield = Zone("battlefield")
    shared_exile = Zone("exile")
    shared_limbo = Zone("limbo")
    shared_stack = Zone("stack")
    player1 = Player(temp_players[1]["name"], temp_players[1]["player_id"], 1, shared_battlefield,
                     shared_exile, shared_limbo, shared_stack, temp_players[1]["deck"])
    player2 = Player(temp_players[2]["name"], temp_players[2]["player_id"], 2, shared_battlefield,
                     shared_exile, shared_limbo, shared_stack, temp_players[2]["deck"])
    with mtga_app.mtga_watch_app.game_lock:
        if mtga_app.mtga_watch_app.player_id == player1.player_id:
            hero = player1
            opponent = player2
        else:
            hero = player2
            opponent = player1
        hero.is_hero = True
        if mtga_app.mtga_watch_app.intend_to_join_game_with:
            hero.original_deck = mtga_app.mtga_watch_app.intend_to_join_game_with
        mtga_app.mtga_watch_app.game = Game(hero, opponent, shared_battlefield, shared_exile, shared_limbo,
                                            shared_stack)