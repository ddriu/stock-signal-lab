from src.app_routes import LOCAL_ROUTES, PRIMARY_ROUTES, ROUTES, TOOLS_GROUPS, route_for_state


def test_primary_routes_are_stable_unique_and_expose_all_public_tasks():
    assert [ROUTES[key].label for key in PRIMARY_ROUTES] == [
        "Hoy", "Cartera", "Radar", "Favoritas", "Más",
    ]
    assert len({route.path for route in ROUTES.values()}) == len(ROUTES)
    reachable = set(PRIMARY_ROUTES)
    for routes in LOCAL_ROUTES.values():
        reachable.update(routes)
    for _, routes in TOOLS_GROUPS:
        reachable.update(routes)
    assert reachable == {key for key, route in ROUTES.items() if not route.admin}


def test_routes_translate_existing_actions_without_recursing_or_leaking_admin():
    for route in ROUTES.values():
        state = {"main_navigation": route.section, **dict(route.selection)}
        assert route_for_state(state, admin=True) == route
        assert route_for_state(state, admin=False).admin is False
    assert route_for_state({"main_navigation": "Analizar"}).key == "radar"
