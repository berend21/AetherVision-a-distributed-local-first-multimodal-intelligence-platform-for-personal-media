import csv
import pickle
from pathlib import Path
from threading import Lock

import numpy as np
from scipy.spatial import cKDTree


MAX_CITY_DISTANCE_KM = 50
EARTH_RADIUS_KM = 6371.0
SUBDIVISION_RADIUS_KM = 1.0


class LocationResolver:

    def __init__(self, pickle_path, admin2_path):

        self.pickle_path = Path(pickle_path)
        self.admin2_path = Path(admin2_path)

        self.cities = None
        self.tree = None

        self.subdivisions = []
        self.subdivision_tree = None

        self.states = {}
        self.countries = {}
        self.municipalities = {}

        self.cities_by_id = {}

        self.cache = {}
        self.lock = Lock()

    def load(self):

        print("Loading city database...")

        with open(self.pickle_path, "rb") as f:
            data = pickle.load(f)

        if len(data) == 6:

            (
                self.cities,
                self.tree,
                self.subdivisions,
                self.subdivision_tree,
                self.states,
                self.countries
            ) = data

        elif len(data) == 4:

            (
                self.cities,
                self.tree,
                self.states,
                self.countries
            ) = data

        else:
            raise ValueError(
                f"Unexpected city database format: {len(data)} objects"
            )

        self.cities_by_id = {
            city["id"]: city
            for city in self.cities
        }

        print("Loading municipalities...")

        with open(self.admin2_path, encoding="utf-8") as f:

            for row in csv.reader(f, delimiter="\t"):

                if len(row) >= 2:
                    self.municipalities[row[0]] = row[1]

        print(f"City database loaded: {len(self.cities):,} cities")
        print(f"Municipalities loaded: {len(self.municipalities):,}")

    @staticmethod
    def to_xyz(lat, lon):

        lat = np.radians(lat)
        lon = np.radians(lon)

        return np.array([
            np.cos(lat) * np.cos(lon),
            np.cos(lat) * np.sin(lon),
            np.sin(lat)
        ])

    def get_municipality(self, place):

        admin2 = place.get("admin2_code")

        if not admin2:
            return None

        key = (
            place["country_code"] + "."
            + place["admin1_code"] + "."
            + admin2
        )

        return self.municipalities.get(key)

    def get_municipality_parent(self, place):

        municipality = self.get_municipality(place)

        if not municipality:
            return None

        municipality_name = municipality.replace(
            "Gemeente ", ""
        )

        for city in self.cities:

            if (
                city["name"].lower() == municipality_name.lower()
                and city["country_code"] == place["country_code"]
                and city["admin1_code"] == place["admin1_code"]
            ):
                return city

        return None

    def reverse_geocode(self, lat, lon):

        if self.tree is None:
            raise RuntimeError("Location database not loaded")

        point = self.to_xyz(lat, lon)

        distance, index = self.tree.query(point)

        distance_km = (
            2 * np.arcsin(min(1.0, float(distance) / 2))
            * EARTH_RADIUS_KM
        )

        if distance_km > MAX_CITY_DISTANCE_KM:

            return {
                "city": None,
                "subdivision": None,
                "region": None,
                "country": None,
                "country_code": None,
                "distance_km": round(distance_km, 2),
                "location_type": "unknown"
            }

        nearest_city = self.cities[int(index)]

        city = nearest_city
        subdivision = None

        parent = self.get_municipality_parent(nearest_city)

        if parent and parent["id"] != nearest_city["id"]:

            city = parent
            subdivision = nearest_city["name"]


        if self.subdivision_tree is not None:

            radius = (
                2 * np.sin(
                    SUBDIVISION_RADIUS_KM
                    / (2 * EARTH_RADIUS_KM)
                )
            )

            indices = self.subdivision_tree.query_ball_point(
                point,
                radius
            )

            nearby = []

            for subdivision_index in indices:

                candidate = self.subdivisions[subdivision_index]

                if (
                    candidate["country_code"] != nearest_city["country_code"]
                    or candidate["admin1_code"] != nearest_city["admin1_code"]
                ):
                    continue

                candidate_point = self.to_xyz(
                    candidate["lat"],
                    candidate["lon"]
                )

                chord_distance = np.linalg.norm(
                    candidate_point - point
                )

                if chord_distance >= distance:
                    continue

                nearby.append((chord_distance, candidate))

            nearby.sort(key=lambda x: x[0])

            for _, candidate in nearby:

                parent = self.get_municipality_parent(candidate)

                if parent is None:
                    continue

                city = parent
                subdivision = candidate["name"]

                break



        country_code = city["country_code"]

        state_key = (
            country_code + "."
            + city["admin1_code"]
        )

        return {
            "city": city["name"],
            "subdivision": subdivision,
            "region": self.states.get(state_key),
            "country": self.countries.get(country_code),
            "country_code": country_code,
            "distance_km": round(distance_km, 2),
            "location_type": "city"
        }

    def cached_reverse_geocode(self, lat, lon):

        key = (round(lat, 4), round(lon, 4))

        with self.lock:

            if key in self.cache:
                return self.cache[key]

        result = self.reverse_geocode(lat, lon)

        with self.lock:

            return self.cache.setdefault(key, result)
