"use client";

import { GoogleMap, Marker, useJsApiLoader } from "@react-google-maps/api";
import { useEffect, useMemo, useRef } from "react";

import type { LatLng, PlaceDetailState, PlaceSummary } from "@/lib/types";

interface MapProps {
  places: PlaceSummary[];
  details: Record<string, PlaceDetailState>;
  selectedPlaceId: string | null;
  mapFocusPlaceId: string | null;
  searchCenter: LatLng;
  searchTargetPlaceId: string | null;
  suggestedPlaceIds: string[];
  onPlaceSelect: (placeId: string | null) => void;
  onNativePlaceSelect: (place: PlaceSummary) => void;
  onMapCenterChange: (center: LatLng) => void;
}

const DEFAULT_CENTER = { lat: 25.2048, lng: 55.2708 };

const MAP_STYLES: google.maps.MapTypeStyle[] = [
  { featureType: "administrative", elementType: "labels.text.fill", stylers: [{ color: "#6d635d" }] },
  { featureType: "landscape", elementType: "geometry", stylers: [{ color: "#f4efe8" }] },
  { featureType: "poi", elementType: "geometry", stylers: [{ color: "#eadfce" }] },
  { featureType: "poi.park", elementType: "geometry", stylers: [{ color: "#d7e4d0" }] },
  { featureType: "road", elementType: "geometry", stylers: [{ color: "#ffffff" }] },
  { featureType: "road", elementType: "labels.text.fill", stylers: [{ color: "#8b8079" }] },
  { featureType: "transit", elementType: "labels.text.fill", stylers: [{ color: "#8b8079" }] },
  { featureType: "water", elementType: "geometry", stylers: [{ color: "#cadbe5" }] },
];

function getMarkerColor(detailState?: PlaceDetailState): string {
  if (!detailState || detailState.status === "loading" || detailState.status === "idle") {
    return "#7d8ea3";
  }
  if (detailState.status === "error") {
    return "#b99277";
  }
  if (!detailState.data.score_summary.meaningful_evidence) {
    return "#92857b";
  }

  switch (detailState.data.score_summary.fit_verdict) {
    case "good_fit":
      return "#2e8b57";
    case "high_risk":
      return "#b63f2c";
    default:
      return "#cf8d2e";
  }
}

export default function Map({
  places,
  details,
  selectedPlaceId,
  mapFocusPlaceId,
  searchCenter,
  searchTargetPlaceId,
  suggestedPlaceIds,
  onPlaceSelect,
  onNativePlaceSelect,
  onMapCenterChange,
}: MapProps) {
  const apiKey = process.env.NEXT_PUBLIC_GOOGLE_MAPS_API_KEY ?? "";
  const { isLoaded } = useJsApiLoader({
    googleMapsApiKey: apiKey,
  });
  const mapRef = useRef<google.maps.Map | null>(null);
  const lastViewportCenter = useRef<string>("");
  const suppressedViewportPublishes = useRef(0);

  const placeIds = useMemo(() => new Set(places.map((place) => place.id)), [places]);
  const suggestionRanks = useMemo(
    () => new globalThis.Map(suggestedPlaceIds.map((placeId, index) => [placeId, index + 1])),
    [suggestedPlaceIds],
  );

  useEffect(() => {
    if (!mapRef.current) {
      return;
    }

    const focusPlace =
      places.find((place) => place.id === mapFocusPlaceId) ??
      places.find((place) => place.id === searchTargetPlaceId) ??
      null;

    const nextCenter = focusPlace?.location ?? searchCenter;
    suppressedViewportPublishes.current = 2;
    mapRef.current.panTo(nextCenter);

    if (focusPlace) {
      mapRef.current.setZoom(Math.max(mapRef.current.getZoom() ?? 14, 16));
    }
  }, [mapFocusPlaceId, places, searchCenter, searchTargetPlaceId]);

  const publishViewportCenter = () => {
    if (suppressedViewportPublishes.current > 0) {
      suppressedViewportPublishes.current -= 1;
      return;
    }

    if (!mapRef.current) {
      return;
    }

    const center = mapRef.current.getCenter();
    if (!center) {
      return;
    }

    const nextCenter = { lat: center.lat(), lng: center.lng() };
    const key = `${nextCenter.lat.toFixed(4)}:${nextCenter.lng.toFixed(4)}`;
    if (lastViewportCenter.current === key) {
      return;
    }

    lastViewportCenter.current = key;
    onMapCenterChange(nextCenter);
  };

  if (!apiKey) {
    return <div className="map-loading">Add `NEXT_PUBLIC_GOOGLE_MAPS_API_KEY` to render the map.</div>;
  }

  if (!isLoaded) {
    return <div className="map-loading">Loading map…</div>;
  }

  return (
    <GoogleMap
      mapContainerStyle={{ width: "100%", height: "100%" }}
      center={searchCenter ?? DEFAULT_CENTER}
      zoom={14}
      onLoad={(instance) => {
        mapRef.current = instance;
        instance.setOptions({
          disableDefaultUI: true,
          zoomControl: true,
          streetViewControl: false,
          fullscreenControl: false,
          mapTypeControl: false,
          clickableIcons: true,
          styles: MAP_STYLES,
          gestureHandling: "greedy",
        });
      }}
      onUnmount={() => {
        mapRef.current = null;
      }}
      onClick={(event) => {
        const placeId = (event as google.maps.IconMouseEvent).placeId;
        if (!placeId) {
          return;
        }
        if (typeof event.stop === "function") {
          event.stop();
        }
        if (placeIds.has(placeId)) {
          onPlaceSelect(placeId);
          return;
        }
        const location = event.latLng
          ? {
              lat: event.latLng.lat(),
              lng: event.latLng.lng(),
            }
          : searchCenter;
        onNativePlaceSelect({
          id: placeId,
          name: "This restaurant",
          address: null,
          location,
          rating: null,
          user_rating_count: null,
          primary_type: null,
        });
      }}
      onDragEnd={publishViewportCenter}
      onZoomChanged={publishViewportCenter}
    >
      {places.map((place) => {
        const isSelected = selectedPlaceId === place.id;
        const isSearchTarget = searchTargetPlaceId === place.id;
        const suggestionRank = suggestionRanks.get(place.id);
        const isSuggested = suggestionRank !== undefined;

        return (
          <Marker
            key={place.id}
            position={place.location}
            onClick={() => onPlaceSelect(place.id)}
            zIndex={isSelected ? 70 : isSuggested ? 60 : isSearchTarget ? 50 : 10}
            label={
              isSuggested
                ? {
                    text: String(suggestionRank),
                    color: "#ffffff",
                    fontSize: "12px",
                    fontWeight: "700",
                  }
                : isSearchTarget
                ? {
                    text: "●",
                    color: "#fffaf4",
                    fontSize: "13px",
                    fontWeight: "700",
                  }
                : undefined
            }
            icon={{
              path: google.maps.SymbolPath.CIRCLE,
              scale: isSelected ? 12 : isSuggested ? 11 : isSearchTarget ? 10 : 8,
              fillColor: isSuggested ? "#176b55" : getMarkerColor(details[place.id]),
              fillOpacity: 1,
              strokeColor: isSuggested || isSearchTarget ? "#f4b942" : "#fffaf4",
              strokeWeight: isSuggested || isSearchTarget ? 4 : 2,
            }}
          />
        );
      })}
    </GoogleMap>
  );
}
