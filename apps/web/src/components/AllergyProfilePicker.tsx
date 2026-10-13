"use client";

import { ALLERGEN_OPTIONS } from "@/lib/allergens";
import type { AllergyTag } from "@/lib/types";

interface AllergyProfilePickerProps {
  selectedAllergens: AllergyTag[];
  onToggle: (allergen: AllergyTag) => void;
}

export default function AllergyProfilePicker({
  selectedAllergens,
  onToggle,
}: AllergyProfilePickerProps) {
  return (
    <div className="allergy-picker">
      {ALLERGEN_OPTIONS.map((option) => {
        const selected = selectedAllergens.includes(option.value);
        return (
          <button
            key={option.value}
            type="button"
            className={`allergy-chip ${selected ? "active" : ""}`}
            aria-pressed={selected}
            onClick={() => onToggle(option.value)}
          >
            <span className="allergy-chip-dot" />
            {option.shortLabel}
          </button>
        );
      })}
    </div>
  );
}

