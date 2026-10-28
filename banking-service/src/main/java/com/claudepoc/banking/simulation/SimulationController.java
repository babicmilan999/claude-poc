package com.claudepoc.banking.simulation;

import com.claudepoc.banking.simulation.IncidentService.Incident;
import com.claudepoc.banking.simulation.IncidentService.IncidentRequest;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

/**
 * POST /simulation/incidents. Exists only with the Spring profile {@code simulation}; without
 * it the path is unmapped and returns 404. Never exposed to the agents as a tool.
 */
@RestController
@Profile("simulation")
public class SimulationController {

    private final IncidentService incidents;

    public SimulationController(IncidentService incidents) {
        this.incidents = incidents;
    }

    @PostMapping("/simulation/incidents")
    public ResponseEntity<Incident> createIncident(@RequestBody(required = false) IncidentRequest request) {
        return ResponseEntity.status(HttpStatus.CREATED).body(incidents.create(request));
    }
}
