import { forwardRef } from "react";

import type { PlaceDetailState, PlaceSummary } from "@/lib/types";

interface PlaceCardProps {
  place: PlaceSummary;
  detailState: PlaceDetailState | undefined;
  selected: boolean;
  onSelect: () => void;
}

function formatRating(rating?: number | null, count?: number | null): string {
  if (!rating) {
    return "Google rating unavailable";
  }
  if (!count) {
    return `${rating.toFixed(1)} Google rating`;
  }
  return `${rating.toFixed(1)} on Google · ${count.toLocaleString()} reviews`;
}

const PlaceCard = forwardRef<HTMLButtonElement, PlaceCardProps>(function PlaceCard(
  { place, detailState, selected, onSelect },
  ref,
) {
  const isReady = detailState?.status === "ready";
  const displayPlace = isReady ? detailState.data : place;
  const menuItems = isReady ? detailState.data.menu?.sections.flatMap((section) => section.items) ?? [] : [];

  return (
    <button ref={ref} type="button" className={`place-card ${selected ? "selected" : ""}`} onClick={onSelect}>
      <p className="place-card-title">{displayPlace.name}</p>
      <p className="place-card-address">{displayPlace.address ?? "Address unavailable"}</p>

      <p className="place-card-meta">{formatRating(displayPlace.rating, displayPlace.user_rating_count)}</p>

      {menuItems.length > 0 && (
        <span className="place-card-menu">
          <span className="place-card-menu-label">Menu preview · {menuItems.length} items</span>
          {menuItems.slice(0, 3).map((item, index) => (
            <span className="place-card-menu-row" key={`${item.name}-${index}`}>
              <span>{item.name}</span>
              {item.price && <span className="place-card-menu-price">{item.price}</span>}
            </span>
          ))}
          <span className="place-card-menu-note">View menu and allergy evidence</span>
        </span>
      )}

      {detailState?.status === "error" && <p className="place-card-error">Details unavailable right now.</p>}

    </button>
  );
});

export default PlaceCard;
