"use client";

import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { menuSources, readableAnswer } from "@/lib/chatPresentation";

import AllergyProfilePicker from "@/components/AllergyProfilePicker";
import AuthBar from "@/components/AuthBar";
import Map from "@/components/Map";
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
  const [nearbyQuestion, setNearbyQuestion] = useState("");
  const conversationRef = useRef<HTMLDivElement>(null);
  const [nearbyConversation, setNearbyConversation] = useState<ConversationTurn[]>([]);
  useEffect(() => {
    const log = conversationRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [nearbyConversation]);
  const [nearbyAnswer, setNearbyAnswer] = useState<NearbySuggestionResponse | null>(null);
  const [chatPhase, setChatPhase] = useState<string | null>(null);
  const [allergiesOpen, setAllergiesOpen] = useState(true);
  const [nearbyAskState, setNearbyAskState] = useState<"idle" | "loading" | "error">("idle");
  const [nearbyAskError, setNearbyAskError] = useState<string | null>(null);
  const [menuLoadingPlaceIds, setMenuLoadingPlaceIds] = useState<Set<string>>(() => new Set());
  const [menuRefreshJobs, setMenuRefreshJobs] = useState<Record<string, MenuRefreshJob>>({});
  const initialized = useRef(false);
  const locationConfirmed = useRef(false);
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
    setChatPhase(null);
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
        selectFirst?: boolean;
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
          setSelectedPlaceId(options.selectFirst === false ? null : firstPlace?.id ?? null);
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
        locationConfirmed.current = true;
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
    setSelectedPlaceId(null);
    setChatPhase("Finding nearby places…");
    let questionCenter = mapCenter;
    if (!locationConfirmed.current) {
      if (locationStatus === "locating") return;
      setLocationStatus("locating");
      setChatPhase("Finding your location…");
      try {
        questionCenter = await new Promise<LatLng>((resolve, reject) => {
          if (!navigator.geolocation) { reject(new Error("Location unavailable")); return; }
          navigator.geolocation.getCurrentPosition(
            ({ coords }) => resolve({ lat: coords.latitude, lng: coords.longitude }), reject,
            { timeout: 8000, maximumAge: 60_000 },
          );
        });
        locationConfirmed.current = true;
        setLocationStatus("idle");
        setMapCenter(questionCenter);
        setSearchCenter(questionCenter);
      } catch {
        setLocationStatus("denied");
        setChatPhase(null);
        setNearbyAskError("Choose an area on the map or use the search bar, then ask again. I couldn’t access your location.");
        return;
      }
    }
    const intentQuery = extractSearchIntent(question, query);
    const intentChanged = intentQuery.toLowerCase() !== (query.trim() || "restaurants").toLowerCase();
    let visiblePlaces = rankedPlaces;
    if (!areaSearchCompleted || canSearchArea || visiblePlaces.length === 0 || intentChanged || questionCenter !== mapCenter) {
      if (intentChanged) {
        setQuery(intentQuery);
      }
      visiblePlaces = await runSearch(intentQuery, questionCenter, selectedAllergens, { selectFirst: false });
      if (visiblePlaces.length === 0) {
        setNearbyAskState("error");
        setChatPhase(null);
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
    setChatPhase("Finding restaurant menus…");
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
            if (suggestion.evidence_status !== "scan_needed" || scansMarked >= 5) {
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
      setChatPhase("Checking dishes for your preferences…");
      const response = await askNearbyPlaces(
        question,
        questionCenter,
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
      setNearbyConversation([...conversation, { role: "assistant" as const, content: response.answer.slice(0, 2000), sources: menuSources(response) }].slice(-10));
      setNearbyQuestion("");
      const runningScans = response.places.filter(
        (suggestion) => suggestion.evidence_status === "scan_running" && suggestion.scan_job_id,
      );
      runningScans.forEach((suggestion) => menuRefreshAttempts.current.add(suggestion.place.id));
      if (allowBackgroundScan && runningScans.length > 0) {
        setChatPhase("Reading menus and finding dishes…");
        void (async () => {
          try {
            const terminalStatuses = new Set(["complete", "failed", "needs_background_refresh"]);
            const deadline = Date.now() + 60_000;
            let completed = false;
            while (Date.now() < deadline) {
              await new Promise((resolve) => window.setTimeout(resolve, 2_000));
              if (requestId !== nearbyRequestSequence.current) return;
              if (Date.now() >= deadline) break;
              const jobs = (await Promise.all(runningScans.map(async (suggestion) => {
                try { return await fetchMenuRefreshJob(suggestion.scan_job_id as string, deadline - Date.now()); }
                catch { return null; }
              }))).filter((job): job is MenuRefreshJob => job !== null);
              if (requestId !== nearbyRequestSequence.current) return;
              setMenuRefreshJobs((current) => ({ ...current, ...Object.fromEntries(jobs.map((job) => [job.place_id, job])) }));
              if (jobs.length === runningScans.length && jobs.every((job) => terminalStatuses.has(job.status))) {
                completed = true;
                break;
              }
            }
            if (requestId !== nearbyRequestSequence.current) return;
            // Keep the original shortlist and conversation; never launch more scans while polling.
            setChatPhase("Putting your recommendations together…");
            const updated = await askNearbyPlaces(question, questionCenter, selectedAllergens,
              response.places.map((suggestion) => suggestion.place), false, intentQuery, conversation);
            if (requestId !== nearbyRequestSequence.current) return;
            const answer = completed ? updated.answer : `${updated.answer} Some menu checks haven’t returned a final result yet; this comparison uses the evidence available so far.`;
            setNearbyAnswer(updated);
            setNearbyConversation([...conversation, { role: "assistant" as const, content: answer.slice(0, 2000), sources: menuSources(updated) }].slice(-10));
          } catch {
            if (requestId !== nearbyRequestSequence.current) return;
            setNearbyConversation([...conversation, { role: "assistant" as const,
              content: "I couldn’t finish comparing the menus. The scans may still complete, but I don’t have a dish recommendation yet. Please try your question again." }].slice(-10));
          } finally {
            if (requestId === nearbyRequestSequence.current) setChatPhase(null);
          }
        })();
      } else {
        setChatPhase(null);
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
      setChatPhase(null);
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
        <button type="button" aria-pressed={mobileView === "results"} onClick={() => { setMobileView("results"); setSelectedPlaceId(null); }}>Chat</button>
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
            locationConfirmed.current = true;
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
            locationConfirmed.current = true;
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
        <section className="glass-panel results-panel map-drawer chat-panel">
          <AuthBar />
          <details className="drawer-filter" open={allergiesOpen} onToggle={(event) => setAllergiesOpen(event.currentTarget.open)}>
            <summary>
              <span>Allergies <small>{selectedAllergens.length} selected</small></span>
              <span className="allergy-collapse-label">{allergiesOpen ? "Collapse ▴" : "Expand ▾"}</span>
            </summary>
            <AllergyProfilePicker selectedAllergens={selectedAllergens} onToggle={toggleAllergen} />
          </details>

          <section className="nearby-rag-panel">
            <div className="nearby-rag-header">
              <div>
                <strong>Ask AllerNav</strong>
              </div>

            </div>
            <details className="chat-sample-questions" open>
              <summary>Sample questions</summary>
              <div className="nearby-sample-queries">
                {SAMPLE_QUESTIONS.map((question) => (
                  <button key={question} type="button" onClick={() => setNearbyQuestion(question)}>
                    {question}
                  </button>
                ))}
              </div>
            </details>
            {nearbyConversation.length > 0 && (
              <div ref={conversationRef} className="nearby-conversation" role="log" aria-label="Conversation with AllerNav">
                {nearbyConversation.map((turn, index) => (
                  <div key={index} className={`nearby-chat-turn ${turn.role}`}>
                    <strong>{turn.role === "user" ? "You" : "AllerNav"}</strong>
                    <p>{turn.role === "assistant" ? readableAnswer(turn.content) : turn.content}</p>
                    {turn.role === "assistant" && !!turn.sources?.length && (
                      <details className="chat-sources">
                        <summary>Menu sources</summary>
                        {turn.sources.filter((source, i, all) => all.findIndex((other) => other.url === source.url) === i).map((source) => (
                          <a key={source.url} href={source.url} target="_blank" rel="noopener noreferrer">{source.label}</a>
                        ))}
                      </details>
                    )}
                  </div>
                ))}
              </div>
            )}
            {nearbyConversation.length === 0 && <p className="chat-welcome">Ask about a dish, menu, or restaurant. I’ll explain what the available evidence supports.</p>}
            {(chatPhase || isSearching) && <div className="chat-loading" role="status" aria-live="polite"><span className="chat-spinner" aria-hidden="true" /><span>{isSearching ? "Finding nearby places…" : chatPhase}</span></div>}
            {nearbyAskError && <p className="panel-error">{nearbyAskError}</p>}
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

          </section>

          {searchError && <p className="panel-error">{searchError}</p>}

        </section>

        {selectedPlaceId && (
        <section className="glass-panel details-panel place-sheet">
          <TrustPanel
            onChooseAnother={() => {
              setSelectedPlaceId(null);
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
