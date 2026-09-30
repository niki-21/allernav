"use client";

import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from "react";

import AllergyProfilePicker from "@/components/AllergyProfilePicker";
import AuthBar from "@/components/AuthBar";
import Map from "@/components/Map";
import PlaceCard from "@/components/PlaceCard";
import SearchBar from "@/components/searchBar";
import TrustPanel from "@/components/TrustPanel";
import {
  ALLERGY_PROFILE_STORAGE_KEY,
  DEFAULT_ALLERGENS,
} from "@/lib/allergens";
import {
  analyzeRestaurant,
  askNearbyPlaces,
  askRestaurant,
  fetchMenuRefreshJob,
  fetchPlaceDetails,
  fetchPlaceMenu,
  menuScanErrorMessage,
  nearbyRagErrorMessage,
  refreshPlaceMenu,
  resolveNearbyCandidateWebsites,
  searchPlaces,
} from "@/lib/api";
import { rankPlaces, shouldShowSearchAreaButton } from "@/lib/placeRanking";
import { applyPlaceDetailError, applyPlaceDetailSuccess, seedPlaceDetailsState } from "@/lib/placeState";
import { extractSearchIntent } from "@/lib/searchIntent";
import type {
  AllergyTag,
  ConversationTurn,
  AskRestaurantResponse,
  LatLng,
  NearbySuggestionResponse,
  MenuRefreshJob,
  PlaceDetailState,
  PlaceMenu,
  PlaceSummary,
} from "@/lib/types";

const DEFAULT_CENTER: LatLng = { lat: 25.2048, lng: 55.2708 };
const DEFAULT_QUERY = "";
const SAMPLE_QUESTIONS = [
  "Which nearby restaurants have the clearest menu evidence for my allergies?",
  "Does chicken biryani contain soy?",
  "Find three nearby restaurants and scan their menus.",
];

function nearbyEvidenceStatus(status: string): string {
  const labels: Record<string, string> = {
    scanned: "Scanned menu",
    scan_needed: "Menu scan needed",
    scan_running: "Scanning menu…",
    scan_failed: "Scan failed — retry",
  };
  return labels[status] ?? "Needs verification";
}

function nearbyRetrievalLabel(mode: string): string {
  if (mode === "general_discovery") {
    return "General restaurant ranking";
  }
  return mode === "scanned_menu_evidence_needed" ? "Scanned menu evidence needed" : "Scanned menu comparison";
}

function candidateName(name?: string | null): string {
  const cleaned = name?.trim();
  return !cleaned || cleaned.toLowerCase() === "selected place" ? "This restaurant" : cleaned;
}

function nearbyBucketSummary(suggestion: NearbySuggestionResponse["places"][number]): string {
  return [
    `${suggestion.possible_lower_risk_count} good option${suggestion.possible_lower_risk_count === 1 ? "" : "s"}`,
    `${suggestion.avoid_count} avoid`,
    `${suggestion.needs_check_count} check`,
  ].join(" · ");
}

function hasScannedMenuEvidence(suggestion: NearbySuggestionResponse["places"][number]): boolean {
  return (
    suggestion.evidence_status === "scanned" &&
    suggestion.menu_item_count > 0 &&
    suggestion.restaurant_fit_score != null
  );
}


function menuIsStale(menu: PlaceMenu | null | undefined): boolean {
  if (!menu || !menu.source_fetched_at) {
    return true;
  }
  const fetchedAt = Date.parse(menu.source_fetched_at);
  return Number.isNaN(fetchedAt) || Date.now() - fetchedAt > 7 * 24 * 60 * 60 * 1000;
}

export default function Home() {
  const [query, setQuery] = useState(DEFAULT_QUERY);
  const [places, setPlaces] = useState<PlaceSummary[]>([]);
  const [detailStates, setDetailStates] = useState<Record<string, PlaceDetailState>>({});
  const [mobileView, setMobileView] = useState<"map" | "results">("results");
  const [selectedPlaceId, setSelectedPlaceId] = useState<string | null>(null);
  const [mapFocusPlaceId, setMapFocusPlaceId] = useState<string | null>(null);
  const [selectedAllergens, setSelectedAllergens] = useState<AllergyTag[]>(DEFAULT_ALLERGENS);
  const [searchCenter, setSearchCenter] = useState<LatLng>(DEFAULT_CENTER);
  const [mapCenter, setMapCenter] = useState<LatLng>(DEFAULT_CENTER);
  const [searchTargetPlaceId, setSearchTargetPlaceId] = useState<string | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [isSearching, setIsSearching] = useState(false);
  const [areaSearchCompleted, setAreaSearchCompleted] = useState(false);
  const [locationStatus, setLocationStatus] = useState<"idle" | "locating" | "denied">("idle");
  const [askResponses, setAskResponses] = useState<Record<string, AskRestaurantResponse>>({});
  const [askingPlaceId, setAskingPlaceId] = useState<string | null>(null);
  const [nearbyQuestion, setNearbyQuestion] = useState("Suggest nearby places to evaluate for my allergies");
  const [nearbyConversation, setNearbyConversation] = useState<ConversationTurn[]>([]);
  const [nearbyAnswer, setNearbyAnswer] = useState<NearbySuggestionResponse | null>(null);
  const [nearbyAskState, setNearbyAskState] = useState<"idle" | "loading" | "error">("idle");
  const [nearbyAskError, setNearbyAskError] = useState<string | null>(null);
  const [menuLoadingPlaceIds, setMenuLoadingPlaceIds] = useState<Set<string>>(() => new Set());
  const [menuRefreshJobs, setMenuRefreshJobs] = useState<Record<string, MenuRefreshJob>>({});
  const initialized = useRef(false);
  const hydratedAllergenKey = useRef<string | null>(null);
  const requestSequence = useRef(0);
  const menuRefreshAttempts = useRef<Set<string>>(new Set());
  const agentAnalysisAttempts = useRef<Set<string>>(new Set());
  const nearbyRequestSequence = useRef(0);
  const nearbyContextRef = useRef<string | null>(null);
  const [profileReady, setProfileReady] = useState(false);

  const resetNearbyRag = useCallback(() => {
    nearbyRequestSequence.current += 1;
    setNearbyAnswer(null);
    setNearbyConversation([]);
    setNearbyAskError(null);
    setNearbyAskState("idle");
  }, []);

  const runMenuRefresh = useCallback(
    async (details: Extract<PlaceDetailState, { status: "ready" }>["data"], forceRefresh = false) => {
      const startedAt = new Date().toISOString();
      setMenuLoadingPlaceIds((current) => new Set(current).add(details.id));
      setMenuRefreshJobs((current) => ({
        ...current,
        [details.id]: {
          id: `running-${details.id}`,
          place_id: details.id,
          status: "running",
          message: "Menu scan is still running.",
          item_count: 0,
          trace: [
            {
              id: "request",
              label: "Start menu discovery",
              status: "running",
              detail: "Menu scan started.",
            },
          ],
          created_at: startedAt,
        },
      }));

      try {
        let job = await refreshPlaceMenu(details.id, {
          placeName: details.name,
          websiteUrl: details.website_uri,
          forceRefresh,
        });
        setMenuRefreshJobs((current) => ({ ...current, [details.id]: job }));
        if ((job.item_count ?? 0) > 0) {
          const fastMenu = await fetchPlaceMenu(details.id, selectedAllergens);
          if (fastMenu) {
            setDetailStates((current) => {
              const currentState = current[details.id];
              if (!currentState || currentState.status !== "ready") {
                return current;
              }
              return {
                ...current,
                [details.id]: { status: "ready", data: { ...currentState.data, menu: fastMenu } },
              };
            });
          }
        }
        const terminalStatuses = new Set(["complete", "failed", "needs_background_refresh"]);
        for (let attempt = 0; attempt < 90 && !terminalStatuses.has(job.status); attempt += 1) {
          await new Promise((resolve) => window.setTimeout(resolve, 2_000));
          job = await fetchMenuRefreshJob(job.id);
          setMenuRefreshJobs((current) => ({ ...current, [details.id]: job }));
        }
        if (!terminalStatuses.has(job.status)) {
          job = {
            ...job,
            status: "needs_background_refresh",
            message: "Menu scan is still running.",
          };
          setMenuRefreshJobs((current) => ({ ...current, [details.id]: job }));
        }
        let refreshedMenu: PlaceMenu | null = null;
        for (let attempt = 0; attempt < 6; attempt += 1) {
          refreshedMenu = await fetchPlaceMenu(details.id, selectedAllergens);
          if (refreshedMenu || job.status !== "complete") {
            break;
          }
          await new Promise((resolve) => window.setTimeout(resolve, 2_000));
        }
        if (refreshedMenu) {
          setDetailStates((current) => {
            const currentState = current[details.id];
            if (!currentState || currentState.status !== "ready") {
              return current;
            }
            return {
              ...current,
              [details.id]: {
                status: "ready",
                data: { ...currentState.data, menu: refreshedMenu },
              },
            };
          });
          void fetchPlaceDetails(details.id, selectedAllergens)
            .then((refreshedDetails) => {
              setDetailStates((current) => {
                const currentState = current[details.id];
                if (!currentState || currentState.status !== "ready") {
                  return current;
                }
                return applyPlaceDetailSuccess(current, details.id, {
                  ...refreshedDetails,
                  agent_recommendation:
                    currentState.data.agent_recommendation ?? refreshedDetails.agent_recommendation,
                });
              });
            })
            .catch(() => undefined);
        }
        if (job.status === "complete" && ["pending", "running"].includes(job.indexing_status ?? "")) {
          void (async () => {
            for (let attempt = 0; attempt < 30; attempt += 1) {
              await new Promise((resolve) => window.setTimeout(resolve, 2_000));
              try {
                const indexJob = await fetchMenuRefreshJob(job.id);
                setMenuRefreshJobs((current) => ({ ...current, [details.id]: indexJob }));
                if (!["pending", "running"].includes(indexJob.indexing_status ?? "")) {
                  break;
                }
              } catch {
                break;
              }
            }
          })();
        }
      } catch (error) {
        const completedAt = new Date().toISOString();
        const message = menuScanErrorMessage(error);
        setMenuRefreshJobs((current) => ({
          ...current,
          [details.id]: {
            id: `failed-${details.id}`,
            place_id: details.id,
            status: "failed",
            message,
            item_count: 0,
            trace: [
              {
                id: "request",
                label: "Run menu discovery",
                status: "failed",
                detail: message,
              },
            ],
            created_at: startedAt,
            completed_at: completedAt,
          },
        }));
      } finally {
        setMenuLoadingPlaceIds((current) => {
          const next = new Set(current);
          next.delete(details.id);
          return next;
        });
      }
    },
    [selectedAllergens],
  );

  useEffect(() => {
    const raw = window.localStorage.getItem(ALLERGY_PROFILE_STORAGE_KEY);
    if (!raw) {
      initialized.current = true;
      setProfileReady(true);
      return;
    }

    try {
      const parsed = JSON.parse(raw) as AllergyTag[];
      if (Array.isArray(parsed)) {
        setSelectedAllergens(parsed);
      }
    } catch {
      window.localStorage.removeItem(ALLERGY_PROFILE_STORAGE_KEY);
    }

    initialized.current = true;
    setProfileReady(true);
  }, []);

  useEffect(() => {
    if (!initialized.current) {
      return;
    }
    window.localStorage.setItem(ALLERGY_PROFILE_STORAGE_KEY, JSON.stringify(selectedAllergens));
  }, [selectedAllergens]);

  const rankedPlaces = useMemo(() => rankPlaces(places, detailStates), [detailStates, places]);

  const selectedPlace = useMemo(
    () => rankedPlaces.find((place) => place.id === selectedPlaceId) ?? null,
    [rankedPlaces, selectedPlaceId],
  );

  const selectedAllergenKey = useMemo(() => selectedAllergens.join("|"), [selectedAllergens]);
  const nearbyContextKey = useMemo(
    () =>
      JSON.stringify({
        query: query.trim().toLowerCase(),
        center: [searchCenter.lat.toFixed(5), searchCenter.lng.toFixed(5)],
        places: places.map((place) => [place.id, place.location.lat, place.location.lng]),
        allergens: selectedAllergens,
      }),
    [searchCenter.lat, searchCenter.lng, places, query, selectedAllergens],
  );
  const canSearchArea = useMemo(() => shouldShowSearchAreaButton(searchCenter, mapCenter), [mapCenter, searchCenter]);
  useEffect(() => {
    if (nearbyContextRef.current !== null && nearbyContextRef.current !== nearbyContextKey) {
      resetNearbyRag();
    }
    nearbyContextRef.current = nearbyContextKey;
  }, [nearbyContextKey, resetNearbyRag]);

  const hydratePlaces = useCallback(async (nextPlaces: PlaceSummary[], allergens: AllergyTag[], sequence: number) => {
    await Promise.allSettled(
      nextPlaces.map(async (place) => {
        try {
          const details = await fetchPlaceDetails(place.id, allergens);
          if (sequence !== requestSequence.current) {
            return;
          }
          setDetailStates((current) => applyPlaceDetailSuccess(current, place.id, details));
          setPlaces((current) =>
            current.map((item) =>
              item.id === place.id
                ? {
                    ...item,
                    name: candidateName(details.name),
                    address: details.address,
                    location: details.location,
                    rating: details.rating,
                    user_rating_count: details.user_rating_count,
                    primary_type: details.primary_type,
                    website_url: details.website_uri,
                  }
                : item,
            ),
          );
        } catch (error) {
          if (sequence !== requestSequence.current) {
            return;
          }
          setDetailStates((current) =>
            applyPlaceDetailError(
              current,
              place.id,
              error instanceof Error ? error.message : "We could not score this place yet.",
            ),
          );
        }
      }),
    );
  }, []);

  const runSearch = useCallback(
    async (
      nextQuery: string,
      nextCenter: LatLng,
      allergens: AllergyTag[],
      options: {
        focusTopResult?: boolean;
      } = {},
    ) => {
      const sequence = ++requestSequence.current;
      resetNearbyRag();
      setIsSearching(true);
      setSearchError(null);

      try {
        const response = await searchPlaces(nextQuery, nextCenter, allergens);
        if (sequence !== requestSequence.current) {
          return [];
        }

        const firstPlace = response.places[0] ?? null;
        const focusedCenter = options.focusTopResult && firstPlace ? firstPlace.location : response.center;

        startTransition(() => {
          setPlaces(response.places);
          setSelectedPlaceId(firstPlace?.id ?? null);
          setMapFocusPlaceId(options.focusTopResult ? firstPlace?.id ?? null : null);
          setSearchTargetPlaceId(options.focusTopResult ? firstPlace?.id ?? null : null);
          setSearchCenter(focusedCenter);
          setMapCenter(focusedCenter);
          setDetailStates(seedPlaceDetailsState(response.places.map((place) => place.id)));
          setAreaSearchCompleted(true);
        });

        void hydratePlaces(response.places, allergens, sequence);
        return response.places;
      } catch (error) {
        if (sequence !== requestSequence.current) {
          return [];
        }
        setSearchError(error instanceof Error ? error.message : "Search failed.");
        return [];
      } finally {
        if (sequence === requestSequence.current) {
          setIsSearching(false);
        }
      }
    },
    [hydratePlaces, resetNearbyRag],
  );

  useEffect(() => {
    if (!profileReady || !places.length) {
      return;
    }
    if (hydratedAllergenKey.current === null) {
      hydratedAllergenKey.current = selectedAllergenKey;
      return;
    }
    if (hydratedAllergenKey.current === selectedAllergenKey) {
      return;
    }
    hydratedAllergenKey.current = selectedAllergenKey;

    const sequence = ++requestSequence.current;
    setDetailStates(seedPlaceDetailsState(places.map((place) => place.id)));
    void hydratePlaces(places, selectedAllergens, sequence);
  }, [hydratePlaces, places, profileReady, selectedAllergens, selectedAllergenKey]);

  useEffect(() => {
    if (!selectedPlaceId || isSearching || nearbyAskState === "loading") {
      return;
    }

    const detailState = detailStates[selectedPlaceId];
    if (!detailState || detailState.status !== "ready") {
      return;
    }

    const details = detailState.data;
    const menuItemCount =
      details.menu?.sections.reduce((count, section) => count + section.items.length, 0) ?? 0;

    if ((!details.menu || menuItemCount === 0 || menuIsStale(details.menu)) && details.website_uri && !menuRefreshAttempts.current.has(details.id)) {
      menuRefreshAttempts.current.add(details.id);
      void runMenuRefresh(details, false);
      return;
    }

    if (selectedAllergens.length === 0) {
      return;
    }

    const agentKey = `${details.id}:${selectedAllergenKey}:${menuItemCount}`;
    if (details.agent_recommendation || agentAnalysisAttempts.current.has(agentKey)) {
      return;
    }

    agentAnalysisAttempts.current.add(agentKey);
    void (async () => {
      try {
        const recommendation = await analyzeRestaurant(
          details.id,
          details.name,
          selectedAllergens,
          details.website_uri,
        );
        setDetailStates((current) => {
          const currentState = current[details.id];
          if (!currentState || currentState.status !== "ready") {
            return current;
          }
          return {
            ...current,
            [details.id]: {
              status: "ready",
              data: {
                ...currentState.data,
                agent_recommendation: recommendation,
              },
            },
          };
        });
      } catch {
        // If FastAPI or LangSmith tracing is not configured, the place panel should still work.
      }
    })();
  }, [detailStates, isSearching, nearbyAskState, runMenuRefresh, selectedAllergens, selectedAllergenKey, selectedPlaceId]);

  const toggleAllergen = (allergen: AllergyTag) => {
    resetNearbyRag();
    setSelectedAllergens((current) => {
      if (current.includes(allergen)) {
        return current.filter((value) => value !== allergen);
      }
      return [...current, allergen];
    });
  };

  const selectPlace = (placeId: string | null) => {
    setSelectedPlaceId(placeId);
    setMapFocusPlaceId(placeId);
  };

  const selectNativePlace = (place: PlaceSummary) => {
    resetNearbyRag();
    setPlaces((current) => [place, ...current.filter((item) => item.id !== place.id)]);
    setSelectedPlaceId(place.id);
    setMapFocusPlaceId(place.id);
    setSearchTargetPlaceId(place.id);
    setDetailStates((current) => ({ ...current, [place.id]: { status: "loading" } }));
    const sequence = ++requestSequence.current;
    void hydratePlaces([place], selectedAllergens, sequence);
  };

  const useCurrentLocation = () => {
    if (!navigator.geolocation) {
      setLocationStatus("denied");
      return;
    }

    setLocationStatus("locating");
    navigator.geolocation.getCurrentPosition(
      (position) => {
        const center = {
          lat: position.coords.latitude,
          lng: position.coords.longitude,
        };
        setLocationStatus("idle");
        setSearchCenter(center);
        setMapCenter(center);
        const nextQuery = query.trim() || "restaurants";
        setQuery(nextQuery);
        void runSearch(nextQuery, center, selectedAllergens, { focusTopResult: true });
      },
      () => setLocationStatus("denied"),
      { enableHighAccuracy: true, timeout: 8000 },
    );
  };

  const askAboutSelectedPlace = async () => {
    if (!selectedPlaceId || !selectedPlace) {
      return;
    }
    setAskingPlaceId(selectedPlaceId);
    try {
      const response = await askRestaurant(selectedPlaceId, selectedPlace.name, selectedAllergens);
      setAskResponses((current) => ({ ...current, [selectedPlaceId]: response }));
    } finally {
      setAskingPlaceId(null);
    }
  };

  const askNearby = async (allowBackgroundScan = false, questionOverride?: string) => {
    const question = (questionOverride ?? nearbyQuestion).trim();
    if (!question) {
      return;
    }
    const intentQuery = extractSearchIntent(question, query);
    const intentChanged = intentQuery.toLowerCase() !== (query.trim() || "restaurants").toLowerCase();
    let visiblePlaces = rankedPlaces;
    if (!areaSearchCompleted || canSearchArea || visiblePlaces.length === 0 || intentChanged) {
      if (intentChanged) {
        setQuery(intentQuery);
      }
      visiblePlaces = await runSearch(intentQuery, mapCenter, selectedAllergens);
      if (visiblePlaces.length === 0) {
        setNearbyAskState("error");
        setNearbyAskError("No restaurants were found in this area. Try moving the map or changing the search.");
        return;
      }
      // The search intentionally establishes the new RAG context; the next render should not cancel it.
      nearbyContextRef.current = null;
    }
    const conversation: ConversationTurn[] = [...nearbyConversation, { role: "user" as const, content: question.slice(0, 2000) }].slice(-9);
    setNearbyConversation(conversation);
    const requestId = ++nearbyRequestSequence.current;
    setNearbyAskState("loading");
    setNearbyAskError(null);
    if (allowBackgroundScan) {
      setNearbyAnswer((current) => {
        if (!current) {
          return current;
        }
        let scansMarked = 0;
        return {
          ...current,
          places: current.places.map((suggestion) => {
            if (suggestion.evidence_status !== "scan_needed" || scansMarked >= 3) {
              return suggestion;
            }
            scansMarked += 1;
            return { ...suggestion, evidence_status: "scan_running" as const };
          }),
        };
      });
    }
    try {
      const candidatePlaces = await resolveNearbyCandidateWebsites(visiblePlaces.slice(0, 8).map((place) => {
        const detailState = detailStates[place.id];
        return {
          ...place,
          name: candidateName(detailState?.status === "ready" ? detailState.data.name : place.name),
          website_url: detailState?.status === "ready" ? detailState.data.website_uri : place.website_url,
        };
      }), selectedAllergens);
      if (requestId !== nearbyRequestSequence.current) {
        return;
      }
      const response = await askNearbyPlaces(
        question,
        mapCenter,
        selectedAllergens,
        candidatePlaces,
        allowBackgroundScan,
        intentQuery,
        conversation,
      );
      if (requestId !== nearbyRequestSequence.current) {
        return;
      }
      setNearbyAnswer(response);
      setNearbyConversation([...conversation, { role: "assistant" as const, content: response.answer.slice(0, 2000) }].slice(-10));
      setNearbyQuestion("");
      const runningScans = response.places.filter(
        (suggestion) => suggestion.evidence_status === "scan_running" && suggestion.scan_job_id,
      );
      runningScans.forEach((suggestion) => menuRefreshAttempts.current.add(suggestion.place.id));
      if (allowBackgroundScan && runningScans.length > 0) {
        void (async () => {
          const terminalStatuses = new Set(["complete", "failed", "needs_background_refresh"]);
          const loadedMenuIds = new Set<string>();
          for (let attempt = 0; attempt < 90; attempt += 1) {
            await new Promise((resolve) => window.setTimeout(resolve, 2_000));
            if (requestId !== nearbyRequestSequence.current) {
              return;
            }
            const jobs = (
              await Promise.all(
                runningScans.map(async (suggestion) => {
                  try {
                    return await fetchMenuRefreshJob(suggestion.scan_job_id as string);
                  } catch {
                    return null;
                  }
                }),
              )
            ).filter((job): job is MenuRefreshJob => job !== null);
            if (requestId !== nearbyRequestSequence.current) {
              return;
            }
            setMenuRefreshJobs((current) => ({
              ...current,
              ...Object.fromEntries(jobs.map((job) => [job.place_id, job])),
            }));

            const menuReadyJobs = jobs.filter(
              (job) => (job.item_count ?? 0) > 0 && !loadedMenuIds.has(job.place_id),
            );
            if (menuReadyJobs.length > 0) {
              menuReadyJobs.forEach((job) => loadedMenuIds.add(job.place_id));
              const refreshedDetails = await Promise.allSettled(
                menuReadyJobs.map(async (job) => {
                  const [details, menu] = await Promise.all([
                    fetchPlaceDetails(job.place_id, selectedAllergens),
                    fetchPlaceMenu(job.place_id, selectedAllergens),
                  ]);
                  return { job, details: { ...details, menu: menu ?? details.menu } };
                }),
              );
              if (requestId !== nearbyRequestSequence.current) {
                return;
              }
              setDetailStates((current) => {
                let next = current;
                refreshedDetails.forEach((result) => {
                  if (result.status === "fulfilled") {
                    next = applyPlaceDetailSuccess(next, result.value.job.place_id, result.value.details);
                  }
                });
                return next;
              });
              const reranked = await askNearbyPlaces(
                question,
                mapCenter,
                selectedAllergens,
                candidatePlaces,
                false,
                intentQuery,
              );
              if (requestId !== nearbyRequestSequence.current) {
                return;
              }
              const stillRunning = new globalThis.Map(
                jobs
                  .filter((job) => !terminalStatuses.has(job.status) && (job.item_count ?? 0) === 0)
                  .map((job) => [job.place_id, job]),
              );
              setNearbyAnswer({
                ...reranked,
                places: reranked.places.map((suggestion) => {
                  const activeJob = stillRunning.get(suggestion.place.id);
                  return activeJob
                    ? {
                        ...suggestion,
                        evidence_status: "scan_running" as const,
                        scan_job_id: activeJob.id,
                        restaurant_fit_score: null,
                      }
                    : suggestion;
                }),
              });
            }
            if (jobs.length === runningScans.length && jobs.every((job) => terminalStatuses.has(job.status))) {
              const reranked = await askNearbyPlaces(
                question,
                mapCenter,
                selectedAllergens,
                candidatePlaces,
                false,
                intentQuery,
              );
              if (requestId !== nearbyRequestSequence.current) {
                return;
              }
              setNearbyAnswer(reranked);
              break;
            }
          }
        })();
      }
    } catch (error) {
      if (requestId !== nearbyRequestSequence.current) {
        return;
      }
      if (allowBackgroundScan) {
        setNearbyAnswer((current) =>
          current
            ? {
                ...current,
                places: current.places.map((suggestion) =>
                  suggestion.evidence_status === "scan_running"
                    ? { ...suggestion, evidence_status: "scan_failed" as const }
                    : suggestion,
                ),
              }
            : current,
        );
      }
      setNearbyAskState("error");
      setNearbyAskError(nearbyRagErrorMessage(error));
      return;
    }
    if (requestId === nearbyRequestSequence.current) {
      setNearbyAskState("idle");
    }
  };

  return (
    <main className={`app-shell mobile-view-${mobileView}`}>
      <nav className="mobile-view-toggle" aria-label="Browse restaurants">
        <button type="button" aria-pressed={mobileView === "map"} onClick={() => setMobileView("map")}>Map</button>
        <button type="button" aria-pressed={mobileView === "results"} onClick={() => { setMobileView("results"); setSelectedPlaceId(null); }}>Results ({rankedPlaces.length})</button>
      </nav>
      <div className="map-layer">
        <Map
          places={places}
          details={detailStates}
          selectedPlaceId={selectedPlaceId}
          mapFocusPlaceId={mapFocusPlaceId}
          searchCenter={searchCenter}
          searchTargetPlaceId={searchTargetPlaceId}
          suggestedPlaceIds={(nearbyAnswer?.places ?? []).slice(0, 3).map((suggestion) => suggestion.place.id)}
          onPlaceSelect={(id) => { selectPlace(id); setMobileView("results"); }}
          onNativePlaceSelect={(place) => { void selectNativePlace(place); setMobileView("results"); }}
          onMapCenterChange={(center) => {
            resetNearbyRag();
            setMapCenter(center);
          }}
        />
      </div>

      <div className="search-slot">
        <SearchBar
          query={query}
          onQueryChange={(nextQuery) => {
            resetNearbyRag();
            setQuery(nextQuery);
          }}
          onSearch={(nextQuery) => {
            setQuery(nextQuery);
            void runSearch(nextQuery, mapCenter, selectedAllergens, { focusTopResult: true });
          }}
          searchCenter={mapCenter}
          isSearching={isSearching}
        />
      </div>

      <button type="button" className="location-button" onClick={useCurrentLocation}>
        {locationStatus === "locating" ? "Locating..." : "Use my location"}
      </button>

      {canSearchArea && (
        <button
          type="button"
          className="area-button"
          onClick={() => void runSearch(query, mapCenter, selectedAllergens)}
        >
          Search this area
        </button>
      )}

      <div className="side-panels">
        <section className="glass-panel results-panel map-drawer">
          <AuthBar />
          <details className="drawer-filter" open>
            <summary>
              <span>Allergies</span>
            </summary>
            <AllergyProfilePicker selectedAllergens={selectedAllergens} onToggle={toggleAllergen} />
          </details>

          <section className="nearby-rag-panel">
            <div className="nearby-rag-header">
              <div>
                <span>Agentic RAG</span>
                <strong>Ask AllerNav</strong>
              </div>
              <small>
                {isSearching
                  ? "Searching area…"
                  : !areaSearchCompleted || canSearchArea || rankedPlaces.length === 0
                    ? "Ready to search this area"
                  : `${Math.min(8, rankedPlaces.length)} search-area candidate${rankedPlaces.length === 1 ? "" : "s"}`}
              </small>
            </div>
            {nearbyConversation.length > 0 && (
              <div className="nearby-conversation" role="log" aria-label="Conversation with AllerNav">
                {nearbyConversation.map((turn, index) => (
                  <div key={index} className={`nearby-chat-turn ${turn.role}`}>
                    <strong>{turn.role === "user" ? "You" : "AllerNav"}</strong>
                    <p>{turn.content}</p>
                  </div>
                ))}
              </div>
            )}
            <form
              className="nearby-rag-form"
              onSubmit={(event) => {
                event.preventDefault();
                void askNearby(true);
              }}
            >
              <textarea
                value={nearbyQuestion}
                onChange={(event) => setNearbyQuestion(event.target.value)}
                rows={2}
                aria-label="Ask AllerNav a question"
                placeholder="Ask a question or follow up…"
                maxLength={2000}
              />
              <button type="submit" disabled={nearbyAskState === "loading" || isSearching}>
                {isSearching ? "Searching..." : nearbyAskState === "loading" ? "Checking..." : "Ask"}
              </button>
            </form>
            <div className="nearby-sample-queries" aria-label="Sample questions">
              {SAMPLE_QUESTIONS.map((sample) => (
                <button
                  type="button"
                  key={sample}
                  onClick={() => {
                    setNearbyQuestion(sample);
                    void askNearby(true, sample);
                  }}
                  disabled={nearbyAskState === "loading" || isSearching}
                >
                  {sample}
                </button>
              ))}
            </div>
            {nearbyAskError && <p className="panel-error">{nearbyAskError}</p>}
            {nearbyAnswer && (
              <div className="nearby-rag-answer">
                {nearbyConversation.length === 0 && <p>{nearbyAnswer.answer}</p>}
                {nearbyAnswer.places.some((place) => place.evidence_status === "scan_running") && (
                  <p role="status">Menu scans are running. Results will update here.</p>
                )}
                {nearbyAnswer.places.length > 0 && (
                  <div className="nearby-rag-suggestions">
                    {nearbyAnswer.places.slice(0, 3).map((suggestion) => {
                      const generalMode = nearbyAnswer.ranking_mode === "general_discovery";
                      const showScore = !generalMode && hasScannedMenuEvidence(suggestion);
                      const possibleItemNames = suggestion.possible_item_names ?? [];
                      const avoidItemNames = suggestion.avoid_item_names ?? [];
                      const scoreTone = showScore
                        ? suggestion.restaurant_fit_score! >= 70
                          ? "good"
                          : suggestion.restaurant_fit_score! >= 45
                            ? "caution"
                            : "risk"
                        : "";
                      return (
                        <button
                          type="button"
                          key={suggestion.place.id}
                          onClick={() => selectPlace(suggestion.place.id)}
                          className="nearby-rag-chip"
                        >
                          <span className="nearby-rag-card-title">
                            <strong>{suggestion.place.name}</strong>
                            {showScore && (
                              <b className={`nearby-score-badge ${scoreTone}`}>{suggestion.restaurant_fit_score}</b>
                            )}
                            {generalMode && suggestion.place.rating != null && (
                              <b className="nearby-rating-badge">{suggestion.place.rating.toFixed(1)}★</b>
                            )}
                          </span>
                          <span className="nearby-rag-card-meta">
                            <b>
                              {generalMode
                                ? suggestion.general_match_label ?? "Nearby restaurant option"
                                : showScore
                                  ? suggestion.restaurant_fit_label
                                  : nearbyEvidenceStatus(suggestion.evidence_status)}
                            </b>
                          </span>
                          {showScore && <small>{nearbyBucketSummary(suggestion)}</small>}
                          {showScore && (
                            <span className="nearby-rag-menu-preview">
                              <small>
                                <b>Good to ask about:</b>{" "}
                                {possibleItemNames.length > 0
                                  ? possibleItemNames.join(", ")
                                  : "No clear options found yet"}
                              </small>
                              <small>
                                <b>Avoid found:</b>{" "}
                                {avoidItemNames.length > 0
                                  ? avoidItemNames.join(", ")
                                  : "none"}
                              </small>
                            </span>
                          )}
                          {suggestion.intent_match === false && suggestion.intent_note && (
                            <small className="nearby-intent-mismatch">{suggestion.intent_note}</small>
                          )}
                          {generalMode && <small>{suggestion.reason}</small>}
                        </button>
                      );
                    })}
                  </div>
                )}
                {nearbyAnswer.ranking_mode === "allergy_fit" &&
                  nearbyAnswer.places.some(hasScannedMenuEvidence) && (
                    <p className="nearby-rag-next-action">
                      Ask staff about sauces, broths, and shared prep before ordering.
                    </p>
                  )}
                {nearbyAnswer.recommended_questions.length > 0 && (
                  <details className="nearby-rag-details">
                    <summary>Staff questions</summary>
                    <ul>
                      {nearbyAnswer.recommended_questions.slice(0, 3).map((question) => (
                        <li key={question}>{question}</li>
                      ))}
                    </ul>
                  </details>
                )}
                {nearbyAnswer.evidence.length > 0 && (
                  <details className="nearby-rag-details">
                    <summary>Evidence details</summary>
                    <div className="nearby-rag-citations">
                      {nearbyAnswer.evidence.slice(0, 3).map((item, index) => (
                        <article key={item.id} className="nearby-rag-citation">
                          <strong>[E{index + 1}] {item.citation_label}</strong>
                          <p>{item.citation_text}</p>
                        </article>
                      ))}
                    </div>
                  </details>
                )}
                <details className="nearby-rag-details">
                  <summary>Technical trace</summary>
                  <small>{nearbyRetrievalLabel(nearbyAnswer.retrieval_mode)}</small>
                </details>
              </div>
            )}
          </section>

          {searchError && <p className="panel-error">{searchError}</p>}

          {rankedPlaces.length > 0 && <button type="button" className="view-results-button" onClick={() => {
            setMobileView("results");
            document.getElementById("restaurant-results")?.scrollIntoView({ behavior: "smooth", block: "start" });
          }}>View restaurant results ({rankedPlaces.length})</button>}
          <div className="results-scroll" id="restaurant-results">
            {rankedPlaces.map((place) => (
              <div key={place.id} className="place-card-shell">
                <PlaceCard
                  place={place}
                  detailState={detailStates[place.id]}
                  selected={place.id === selectedPlaceId}
                  onSelect={() => selectPlace(place.id)}
                />
              </div>
            ))}

            {!rankedPlaces.length && !searchError && (
              <p className="empty-results">
                Search for restaurants, use your location, or ask AllerNav to suggest places in the current map area.
                {locationStatus === "denied" ? " Location permission was not available." : ""}
              </p>
            )}
          </div>
        </section>

        {selectedPlaceId && (
        <section className="glass-panel details-panel place-sheet">
          <TrustPanel
            onChooseAnother={() => {
              setSelectedPlaceId(null);
              document.getElementById("restaurant-results")?.scrollIntoView({ behavior: "smooth" });
            }}
            place={selectedPlace}
            detailState={selectedPlaceId ? detailStates[selectedPlaceId] : undefined}
            askResponse={selectedPlaceId ? askResponses[selectedPlaceId] : null}
            isAskingRestaurant={askingPlaceId === selectedPlaceId}
            isMenuLoading={selectedPlaceId ? menuLoadingPlaceIds.has(selectedPlaceId) : false}
            menuRefreshJob={selectedPlaceId ? menuRefreshJobs[selectedPlaceId] : undefined}
            onRefreshMenu={() => {
              if (!selectedPlaceId) {
                return;
              }
              const detailState = detailStates[selectedPlaceId];
              if (!detailState || detailState.status !== "ready") {
                return;
              }
              menuRefreshAttempts.current.add(selectedPlaceId);
              void runMenuRefresh(detailState.data, true);
            }}
            onAskRestaurant={askAboutSelectedPlace}
            onRetry={() => {
              if (!selectedPlaceId) {
                return;
              }
              setDetailStates((current) => ({ ...current, [selectedPlaceId]: { status: "loading" } }));
              void hydratePlaces(
                rankedPlaces.filter((place) => place.id === selectedPlaceId),
                selectedAllergens,
                requestSequence.current,
              );
            }}
          />
        </section>
        )}
      </div>
    </main>
  );
}
