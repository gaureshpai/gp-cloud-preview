package godaddyv3

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/libdns/libdns"
)

func TestAppendRecordsUsesGoDaddyPATAndKeepsTXTValuesSeparate(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-pat" {
			t.Fatalf("unexpected Authorization header")
		}
		if r.Method != http.MethodPost || r.URL.Path != "/v3/domains/zones/example.com/dns-records" {
			t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		var input recordInput
		if err := json.NewDecoder(r.Body).Decode(&input); err != nil {
			t.Fatal(err)
		}
		if input.Type != "TXT" || input.Name != "_acme-challenge.preview" || input.Data != "proof" || input.TTL != 600 {
			t.Fatalf("unexpected DNS record: %#v", input)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write([]byte(`{"recordId":"created-1","type":"TXT","name":"_acme-challenge.preview","data":"proof","ttl":600}`))
	}))
	defer server.Close()

	provider := testProvider(server.URL)
	created, err := provider.AppendRecords(context.Background(), "example.com.", []libdns.Record{
		libdns.TXT{Name: "_acme-challenge.preview.example.com.", Text: "proof"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(created) != 1 {
		t.Fatalf("got %d created records, want 1", len(created))
	}
	actual := created[0].(libdns.TXT)
	if actual.ProviderData != "created-1" || actual.TTL != minimumTTL {
		t.Fatalf("unexpected created record metadata: %#v", actual)
	}
}

func TestDeleteRecordsDeletesOnlyMatchingTXTValue(t *testing.T) {
	var deleted []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer test-pat" {
			t.Fatalf("unexpected Authorization header")
		}
		if r.Method == http.MethodGet {
			if r.URL.Query().Get("type") != "TXT" || r.URL.Query().Get("name") != "_acme-challenge.preview" {
				t.Fatalf("unexpected list filter: %s", r.URL.RawQuery)
			}
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"items":[{"recordId":"keep","type":"TXT","name":"_acme-challenge.preview","data":"other","ttl":600},{"recordId":"delete","type":"TXT","name":"_acme-challenge.preview","data":"proof","ttl":600}]}`))
			return
		}
		if r.Method == http.MethodDelete {
			deleted = append(deleted, strings.TrimPrefix(r.URL.Path, "/v3/domains/zones/example.com/dns-records/"))
			w.WriteHeader(http.StatusNoContent)
			return
		}
		t.Fatalf("unexpected request: %s %s", r.Method, r.URL.Path)
	}))
	defer server.Close()

	provider := testProvider(server.URL)
	removed, err := provider.DeleteRecords(context.Background(), "example.com", []libdns.Record{
		libdns.TXT{Name: "_acme-challenge.preview", Text: "proof"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(removed) != 1 || len(deleted) != 1 || deleted[0] != "delete" {
		t.Fatalf("deleted %v, got records %#v", deleted, removed)
	}
}

func TestGetRecordsPaginatesAndReturnsTXTOnly(t *testing.T) {
	var pages []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		pages = append(pages, r.URL.Query().Get("page"))
		items := make([]record, 0, pageSize)
		if r.URL.Query().Get("page") == "1" {
			for range pageSize {
				items = append(items, record{ID: "id", Type: "TXT", Name: "_acme-challenge", Data: "proof", TTL: 600})
			}
		} else {
			items = append(items, record{ID: "ignored", Type: "A", Name: "@", Data: "192.0.2.1", TTL: 600})
			items = append(items, record{ID: "txt-2", Type: "TXT", Name: "other", Data: "proof-2", TTL: 600})
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(recordsPage{Items: items})
	}))
	defer server.Close()

	provider := testProvider(server.URL)
	got, err := provider.GetRecords(context.Background(), "example.com.")
	if err != nil {
		t.Fatal(err)
	}
	if len(pages) != 2 || pages[0] != "1" || pages[1] != "2" {
		t.Fatalf("unexpected pages requested: %v", pages)
	}
	if len(got) != pageSize+1 {
		t.Fatalf("got %d records, want %d TXT records", len(got), pageSize+1)
	}
	if got[0].RR().TTL != 600*time.Second {
		t.Fatalf("unexpected TXT TTL: %s", got[0].RR().TTL)
	}
}

func TestRelativeNameRejectsPathAndNormalizesFQDN(t *testing.T) {
	got, err := relativeName("example.com", "_acme-challenge.preview.example.com.")
	if err != nil || got != "_acme-challenge.preview" {
		t.Fatalf("relativeName returned %q, %v", got, err)
	}
	if _, err := relativeName("example.com", "../other"); err == nil {
		t.Fatal("expected unsafe record name to be rejected")
	}
	if _, err := normalizeZone("example.com/path"); err == nil {
		t.Fatal("expected unsafe zone to be rejected")
	}
}

func testProvider(baseURL string) *Provider {
	return &Provider{APIToken: "test-pat", baseURL: baseURL + "/v3/domains/zones", client: http.DefaultClient}
}
