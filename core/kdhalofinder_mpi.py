# from guppy import hpy; hp = hpy()
import pickle
from collections import defaultdict

import mpi4py
import numpy as np
from mpi4py import MPI
from scipy.spatial import cKDTree

mpi4py.rc.recv_mprobe = False
import astropy.constants as const
import astropy.units as u
import time
import h5py
import sys
import utilities
import halo_properties as hprop

# Initializations and preliminaries
comm = MPI.COMM_WORLD  # get MPI communicator object
size = comm.size  # total number of processes
rank = comm.rank  # rank of this process
status = MPI.Status()  # get MPI status object


def find_halos(tree, pos, linkl, npart):
    """ A function which creates a KD-Tree using scipy.CKDTree and queries it to find particles
    neighbours within a linking length. From This neighbour information particles are assigned
    halo IDs and then returned.
    :param pos: The particle position vectors array.
    :param npart: The number of particles in the simulation.
    :param boxsize: The length of the simulation box along one axis.
    :param batchsize: The batchsize for each query to the KD-Tree (see Docs for more information).
    :param linkl: The linking length.
    :param debug_npart: Number of particles to sort during debugging if required.
    :return: part_haloids: The array of halo IDs assigned to each particle (where the index is the particle ID)
             assigned_parts: A dictionary containing the particle IDs assigned to each halo.
             final_halo_ids: An array of final halo IDs (where the index is the initial halo ID and value is the
             final halo ID.
             query_func: The tree query object assigned to a variable.
    """

    # =============== Initialise The Halo Finder Variables/Arrays and The KD-Tree ===============

    # Initialise the arrays and dictionaries for storing halo data
    part_haloids = np.full(npart, -1,
                           dtype=np.int32)  # halo ID containing each particle
    assigned_parts = defaultdict(
        set)  # dictionary to store the particles in a particular halo
    # A dictionary where each key is an initial halo ID and the item is the halo IDs it has been linked with
    linked_halos_dict = defaultdict(set)
    final_halo_ids = np.full(npart, -1,
                             dtype=np.int32)  # final halo ID of linked halos (index is initial halo ID)

    # Initialise the halo ID counter (IDs start at 0)
    ihaloid = -1

    # Assign the query object to a variable to save time on repeated calls
    query_func = tree.query_ball_point

    # =============== Assign Particles To Initial Halos ===============

    # Query the tree returning a list of lists
    query = query_func(pos, r=linkl)

    # Loop through query results assigning initial halo IDs
    for query_part_inds in iter(query):

        # Convert the particle index list to an array for ease of use
        query_part_inds = np.array(query_part_inds, copy=False, dtype=int)

        # Assert that the query particle is returned by the tree query. Otherwise the program fails
        assert query_part_inds.size != 0, 'Must always return particle that you are sitting on'

        # Find only the particles not already in a halo
        new_parts = query_part_inds[
            np.where(part_haloids[query_part_inds] == -1)]

        # If only one particle is returned by the query and it is new it is a 'single particle halo'
        if new_parts.size == query_part_inds.size == 1:

            # Assign the 'single particle halo' halo ID to the particle
            part_haloids[new_parts] = -2

        # If all particles are new increment the halo ID and assign a new halo
        elif new_parts.size == query_part_inds.size:

            # Increment the halo ID by 1 (initialising a new halo)
            ihaloid += 1

            # Assign the new halo ID to the particles
            part_haloids[new_parts] = ihaloid
            assigned_parts[ihaloid] = set(new_parts)

            # Assign the final halo ID to be the newly assigned halo ID
            final_halo_ids[ihaloid] = ihaloid
            linked_halos_dict[ihaloid] = {ihaloid}

        else:

            # ===== Get the 'final halo ID value' =====

            # Extract the IDs of halos returned by the query
            contained_halos = part_haloids[query_part_inds]

            # Get only the unique halo IDs
            uni_cont_halos = np.unique(contained_halos)

            # # Assure no single particle halos are included in the query results
            # assert any(uni_cont_halos != -2), 'Single particle halos should never be found'

            # Remove any unassigned halos
            uni_cont_halos = uni_cont_halos[np.where(uni_cont_halos != -1)]

            # If there is only one halo ID returned avoid the slower code to combine IDs
            if uni_cont_halos.size == 1:

                # Get the list of linked halos linked to the current halo from the linked halo dictionary
                linked_halos = linked_halos_dict[uni_cont_halos[0]]

            else:

                # Get all the linked halos from dictionary so as to not miss out any halos IDs that are linked
                # but not returned by this particular query
                linked_halos_set = set()  # initialise linked halo set
                linked_halos = linked_halos_set.union(
                    *[linked_halos_dict.get(halo) for halo in uni_cont_halos])

            # Find the minimum halo ID to make the final halo ID
            final_ID = min(linked_halos)

            # Assign the linked halos to all the entries in the linked halos dictionary
            linked_halos_dict.update(
                dict.fromkeys(list(linked_halos), linked_halos))

            # Assign the final halo ID array entries
            final_halo_ids[list(linked_halos)] = final_ID

            # Assign new particles to the particle halo IDs array with the final ID
            part_haloids[new_parts] = final_ID

            # Assign the new particles to the final ID in halo dictionary entry
            assigned_parts[final_ID].update(new_parts)

    # =============== Reassign All Halos To Their Final Halo ID ===============

    # Loop over initial halo IDs reassigning them to the final halo ID
    for halo_id in list(assigned_parts.keys()):

        # Extract the final halo value
        final_ID = final_halo_ids[halo_id]

        # Assign this final ID to all the particles in the initial halo ID
        part_haloids[list(assigned_parts[halo_id])] = final_ID
        assigned_parts[final_ID].update(assigned_parts[halo_id])

        # Remove non final ID entry from dictionary to save memory
        if halo_id != final_ID:
            del assigned_parts[halo_id]

    return part_haloids, assigned_parts


def find_subhalos(halo_pos, sub_linkl):
    """ A function that finds subhalos within host halos by applying the same KD-Tree algorithm at a
    higher overdensity.
    :param halo_pos: The position vectors of particles within the host halo.
    :param sub_llcoeff: The linking length coefficient used to define a subhalo.
    :param boxsize: The length of the simulation box along one axis.
    :param npart: The number of particles in the simulation.
    :return: part_subhaloids: The array of subhalo IDs assigned to each particle in the host halo
             (where the index is the particle ID).
             assignedsub_parts: A dictionary containing the particle IDs assigned to each subhalo.
    """

    # =============== Initialise The Halo Finder Variables/Arrays and The KD-Tree ===============

    # Initialise arrays and dictionaries for storing subhalo data
    part_subhaloids = np.full(halo_pos.shape[0], -1,
                              dtype=int)  # subhalo ID of the halo each particle is in
    assignedsub_parts = defaultdict(
        set)  # Dictionary to store the particles in a particular subhalo
    # A dictionary where each key is an initial subhalo ID and the item is the subhalo IDs it has been linked with
    linked_subhalos_dict = defaultdict(set)
    # Final subhalo ID of linked halos (index is initial subhalo ID)
    final_subhalo_ids = np.full(halo_pos.shape[0], -1, dtype=int)

    # Initialise subhalo ID counter (IDs start at 0)
    isubhaloid = -1

    npart = halo_pos.shape[0]

    # Build the halo kd tree
    tree = cKDTree(halo_pos, leafsize=32, compact_nodes=True,
                   balanced_tree=True)

    query = tree.query_ball_point(halo_pos, r=sub_linkl)

    # Loop through query results
    for query_part_inds in iter(query):

        # Convert the particle index list to an array for ease of use.
        query_part_inds = np.array(query_part_inds, copy=False, dtype=int)

        # Assert that the query particle is returned by the tree query. Otherwise the program fails
        assert query_part_inds.size != 0, 'Must always return particle that you are sitting on'

        # Find only the particles not already in a halo
        new_parts = query_part_inds[
            np.where(part_subhaloids[query_part_inds] == -1)]

        # If only one particle is returned by the query and it is new it is a 'single particle subhalo'
        if new_parts.size == query_part_inds.size == 1:

            # Assign the 'single particle subhalo' subhalo ID to the particle
            part_subhaloids[new_parts] = -2

        # If all particles are new increment the subhalo ID and assign a new subhalo ID
        elif new_parts.size == query_part_inds.size:

            # Increment the subhalo ID by 1 (initialise new halo)
            isubhaloid += 1

            # Assign the subhalo ID to the particles
            part_subhaloids[new_parts] = isubhaloid
            assignedsub_parts[isubhaloid] = set(new_parts)

            # Assign the final subhalo ID to be the newly assigned subhalo ID
            final_subhalo_ids[isubhaloid] = isubhaloid
            linked_subhalos_dict[isubhaloid] = {isubhaloid}

        else:

            # ===== Get the 'final subhalo ID value' =====

            # Extract the IDs of subhalos returned by the query
            contained_subhalos = part_subhaloids[query_part_inds]

            # Return only the unique subhalo IDs
            uni_cont_subhalos = np.unique(contained_subhalos)

            # # Assure no single particles are returned by the query
            # assert any(uni_cont_subhalos != -2), 'Single particle halos should never be found'

            # Remove any unassigned subhalos
            uni_cont_subhalos = uni_cont_subhalos[
                np.where(uni_cont_subhalos != -1)]

            # If there is only one subhalo ID returned avoid the slower code to combine IDs
            if uni_cont_subhalos.size == 1:

                # Get the list of linked subhalos linked to the current subhalo from the linked subhalo dictionary
                linked_subhalos = linked_subhalos_dict[uni_cont_subhalos[0]]

            else:

                # Get all linked subhalos from the dictionary so as to not miss out any subhalos IDs that are linked
                # but not returned by this particular query
                linked_subhalos_set = set()  # initialise linked subhalo set
                linked_subhalos = linked_subhalos_set.union(
                    *[linked_subhalos_dict.get(subhalo)
                      for subhalo in uni_cont_subhalos])

            # Find the minimum subhalo ID to make the final subhalo ID
            final_ID = min(linked_subhalos)

            # Assign the linked subhalos to all the entries in the linked subhalos dict
            linked_subhalos_dict.update(
                dict.fromkeys(list(linked_subhalos), linked_subhalos))

            # Assign the final subhalo array
            final_subhalo_ids[list(linked_subhalos)] = final_ID

            # Assign new parts to the subhalo IDs with the final ID
            part_subhaloids[new_parts] = final_ID

            # Assign the new particles to the final ID particles in subhalo dictionary entry
            assignedsub_parts[final_ID].update(new_parts)

    # =============== Reassign All Subhalos To Their Final Subhalo ID ===============

    # Loop over initial subhalo IDs reassigning them to the final subhalo ID
    for subhalo_id in list(assignedsub_parts.keys()):

        # Extract the final subhalo value
        final_ID = final_subhalo_ids[subhalo_id]

        # Assign this final ID to all the particles in the initial subhalo ID
        part_subhaloids[list(assignedsub_parts[subhalo_id])] = final_ID
        assignedsub_parts[final_ID].update(assignedsub_parts[subhalo_id])

        # Remove non final ID entry from dictionary to save memory
        if subhalo_id != final_ID:
            assignedsub_parts.pop(subhalo_id)

    return part_subhaloids, assignedsub_parts


def find_phase_space_halos(halo_phases):
    # =============== Initialise The Halo Finder Variables/Arrays and The KD-Tree ===============

    # Initialise arrays and dictionaries for storing halo data
    phase_part_haloids = np.full(halo_phases.shape[0], -1,
                                 dtype=int)  # halo ID of the halo each particle is in
    phase_assigned_parts = {}  # Dictionary to store the particles in a particular halo
    # A dictionary where each key is an initial subhalo ID and the item is the subhalo IDs it has been linked with
    phase_linked_halos_dict = {}
    # Final halo ID of linked halos (index is initial halo ID)
    final_phasehalo_ids = np.full(halo_phases.shape[0], -1, dtype=int)

    # Initialise subhalo ID counter (IDs start at 0)
    ihaloid = -1

    # Initialise the halo kd tree in 6D phase space
    halo_tree = cKDTree(halo_phases, leafsize=16, compact_nodes=True,
                        balanced_tree=True)

    query = halo_tree.query_ball_point(halo_phases, r=np.sqrt(2))

    # Loop through query results assigning initial halo IDs
    for query_part_inds in iter(query):

        query_part_inds = np.array(query_part_inds, dtype=int)

        # If only one particle is returned by the query and it is new it is a 'single particle halo'
        if query_part_inds.size == 1:
            # Assign the 'single particle halo' halo ID to the particle
            phase_part_haloids[query_part_inds] = -2

        # # Find the previous halo ID associated to these particles
        # this_halo_ids = halo_ids[query_part_inds]
        # uni_this_halo_ids = set(this_halo_ids)
        # if len(uni_this_halo_ids) > 1:
        #     query_part_inds = query_part_inds[np.where(this_halo_ids == this_halo_ids[0])]

        # Find only the particles not already in a halo
        new_parts = query_part_inds[
            np.where(phase_part_haloids[query_part_inds] < 0)]

        # If all particles are new increment the halo ID and assign a new halo
        if new_parts.size == query_part_inds.size:

            # Increment the halo ID by 1 (initialising a new halo)
            ihaloid += 1

            # Assign the new halo ID to the particles
            phase_part_haloids[new_parts] = ihaloid
            phase_assigned_parts[ihaloid] = set(new_parts)

            # Assign the final halo ID to be the newly assigned halo ID
            final_phasehalo_ids[ihaloid] = ihaloid
            phase_linked_halos_dict[ihaloid] = {ihaloid}

        else:

            # ===== Get the 'final halo ID value' =====

            # Extract the IDs of halos returned by the query
            contained_halos = phase_part_haloids[query_part_inds]

            # Get only the unique halo IDs
            uni_cont_halos = np.unique(contained_halos)

            # Remove any unassigned halos
            uni_cont_halos = uni_cont_halos[np.where(uni_cont_halos >= 0)]

            # If there is only one halo ID returned avoid the slower code to combine IDs
            if uni_cont_halos.size == 1:

                # Get the list of linked halos linked to the current halo from the linked halo dictionary
                linked_halos = phase_linked_halos_dict[uni_cont_halos[0]]

            elif uni_cont_halos.size == 0:
                continue

            else:

                # Get all the linked halos from dictionary so as to not miss out any halos IDs that are linked
                # but not returned by this particular query
                linked_halos_set = set()  # initialise linked halo set
                linked_halos = linked_halos_set.union(
                    *[phase_linked_halos_dict.get(halo)
                      for halo in uni_cont_halos])

            # Find the minimum halo ID to make the final halo ID
            final_ID = min(linked_halos)

            # Assign the linked halos to all the entries in the linked halos dictionary
            phase_linked_halos_dict.update(
                dict.fromkeys(list(linked_halos), linked_halos))

            # Assign the final halo ID array entries
            final_phasehalo_ids[list(linked_halos)] = final_ID

            # Assign new particles to the particle halo IDs array with the final ID
            phase_part_haloids[new_parts] = final_ID

            # Assign the new particles to the final ID in halo dictionary entry
            phase_assigned_parts[final_ID].update(new_parts)

    # =============== Reassign All Halos To Their Final Halo ID ===============

    # Loop over initial halo IDs reassigning them to the final halo ID
    for halo_id in list(phase_assigned_parts.keys()):

        # Extract the final halo value
        final_ID = final_phasehalo_ids[halo_id]

        # Assign this final ID to all the particles in the initial halo ID
        phase_part_haloids[list(phase_assigned_parts[halo_id])] = final_ID
        phase_assigned_parts[final_ID].update(phase_assigned_parts[halo_id])

        # Remove non final ID entry from dictionary to save memory
        if halo_id != final_ID:
            phase_assigned_parts.pop(halo_id)

    return phase_part_haloids, phase_assigned_parts


halo_energy_calc = utilities.halo_energy_calc_exact


def spatial_node_task(thisTask, pos, tree, linkl, npart):
    # =============== Run The Halo Finder And Reduce The Output ===============

    # Run the halo finder for this snapshot at the host linking length and get the spatial catalog
    task_part_haloids, task_assigned_parts = find_halos(tree, pos, linkl,
                                                        npart)

    # Get the positions
    halo_pids = {}
    while len(task_assigned_parts) > 0:
        item = task_assigned_parts.popitem()
        halo, part_inds = item
        # halo_pids[(thisTask, halo)] = np.array(list(part_inds))
        halo_pids[(thisTask, halo)] = frozenset(part_inds)

    return halo_pids


def get_real_host_halos(sim_halo_pids, halo_poss, halo_vels, boxsize,
                        vlinkl_halo_indp, linkl, pmass, ini_vlcoeff,
                        decrement, redshift, G, h, soft, min_vlcoeff, cosmo):
    # Initialise dicitonaries to store results
    results = {}

    # Define the comparison particle as the maximum position
    # in the current dimension
    max_part_pos = halo_poss.max(axis=0)

    # Compute all the halo particle separations from the maximum position
    sep = max_part_pos - halo_poss

    # If any separations are greater than 50% the boxsize
    # (i.e. the halo is split over the boundary)
    # bring the particles at the lower boundary together with
    # the particles at the upper boundary (ignores halos where
    # constituent particles aren't separated by at least 50% of the boxsize)
    # *** Note: fails if halo's extent is greater than 50% of
    # the boxsize in any dimension ***
    halo_poss[np.where(sep > 0.5 * boxsize)] += boxsize

    not_real_pids = {}
    candidate_halos = {0: {"pos": halo_poss,
                           "vel": halo_vels,
                           "pid": sim_halo_pids,
                           "vlcoeff": ini_vlcoeff}}
    candidateID = 0
    thisresultID = 0

    while len(candidate_halos) > 0:

        key, candidate_halo = candidate_halos.popitem()

        halo_poss = candidate_halo["pos"]
        halo_vels = candidate_halo["vel"]
        sim_halo_pids = candidate_halo["pid"]
        halo_npart = sim_halo_pids.size
        new_vlcoeff = candidate_halo["vlcoeff"]

        new_vlcoeff -= decrement * new_vlcoeff

        # Define the phase space linking length
        vlinkl = new_vlcoeff * vlinkl_halo_indp \
                 * pmass ** (1 / 3) * halo_npart ** (1 / 3)

        # Add the hubble flow to the velocities
        # *** NOTE: this includes a gadget factor of a^-1/2 ***
        ini_cent = np.mean(halo_poss, axis=0)
        sep = cosmo.H(redshift).value * (halo_poss - ini_cent) * (
                    1 + redshift) ** -0.5
        halo_vels += sep

        # Define the phase space vectors for this halo
        halo_phases = np.concatenate((halo_poss / linkl,
                                      halo_vels / vlinkl), axis=1)

        # Query these particles in phase space to find distinct bound halos
        part_haloids, assigned_parts = find_phase_space_halos(halo_phases)

        not_real_pids = {}

        thiscontID = 0
        while len(assigned_parts) > 0:

            # Get the next halo from the dictionary and ensure
            # it has more than 10 particles
            key, val = assigned_parts.popitem()
            if len(val) < 10:
                continue

            # Extract halo particle data
            this_halo_pids = list(val)
            halo_npart = len(this_halo_pids)
            this_halo_pos = halo_poss[this_halo_pids, :]
            this_halo_vel = halo_vels[this_halo_pids, :]
            this_sim_halo_pids = sim_halo_pids[this_halo_pids]

            # Compute the centred positions and velocities
            mean_halo_pos = this_halo_pos.mean(axis=0)
            mean_halo_vel = this_halo_vel.mean(axis=0)
            this_halo_pos -= mean_halo_pos
            this_halo_vel -= mean_halo_vel

            # Compute halo's energy
            halo_energy, KE, GE = halo_energy_calc(this_halo_pos,
                                                   this_halo_vel,
                                                   halo_npart,
                                                   pmass, redshift,
                                                   G, h, soft)

            if KE / GE <= 1:

                # Get rms radii from the centred position and velocity
                r = hprop.rms_rad(this_halo_pos)
                vr = hprop.rms_rad(this_halo_vel)

                # Compute the velocity dispersion
                veldisp3d, veldisp1d = hprop.vel_disp(this_halo_vel)

                # Define "masses" for property computation
                masses = np.ones(len(this_sim_halo_pids))

                # Compute maximal rotational velocity
                vmax = hprop.vmax(this_halo_pos, masses, G)

                # Calculate half mass radius in position and velocity space
                hmr = hprop.half_mass_rad(this_halo_pos, masses)
                hmvr = hprop.half_mass_rad(this_halo_vel, masses)

                # Define realness flag
                real = True

                results[thisresultID] = {'pids': this_sim_halo_pids,
                                         'npart': halo_npart, 'real': real,
                                         'mean_halo_pos': mean_halo_pos,
                                         'mean_halo_vel': mean_halo_vel,
                                         'halo_energy': halo_energy,
                                         'KE': KE, 'GE': GE,
                                         "rms_r": r, "rms_vr": vr,
                                         "veldisp3d": veldisp3d,
                                         "veldisp1d": veldisp1d,
                                         "vmax": vmax,
                                         "hmr": hmr,
                                         "hmvr": hmvr}

                thisresultID += 1

            elif KE / GE > 1 and new_vlcoeff <= min_vlcoeff:

                # Get rms radii from the centred position and velocity
                r = hprop.rms_rad(this_halo_pos)
                vr = hprop.rms_rad(this_halo_vel)

                # Compute the velocity dispersion
                veldisp3d, veldisp1d = hprop.vel_disp(this_halo_vel)

                # Define "masses" for property computation
                masses = np.ones(len(this_sim_halo_pids))

                # Compute maximal rotational velocity
                vmax = hprop.vmax(this_halo_pos, masses, G)

                # Calculate half mass radius in position and velocity space
                hmr = hprop.half_mass_rad(this_halo_pos, masses)
                hmvr = hprop.half_mass_rad(this_halo_vel, masses)

                # Define realness flag
                real = False

                results[thisresultID] = {'pids': this_sim_halo_pids,
                                         'npart': halo_npart, 'real': real,
                                         'mean_halo_pos': mean_halo_pos,
                                         'mean_halo_vel': mean_halo_vel,
                                         'halo_energy': halo_energy,
                                         'KE': KE, 'GE': GE,
                                         "rms_r": r, "rms_vr": vr,
                                         "veldisp3d": veldisp3d,
                                         "veldisp1d": veldisp1d,
                                         "vmax": vmax,
                                         "hmr": hmr,
                                         "hmvr": hmvr}

                thisresultID += 1

            else:
                not_real_pids[thiscontID] = this_halo_pids
                candidate_halos[candidateID] = {"pos": (this_halo_pos
                                                        + mean_halo_pos),
                                                "vel": (this_halo_vel
                                                        + mean_halo_vel),
                                                "pid": this_sim_halo_pids,
                                                "vlcoeff": new_vlcoeff}

                candidateID += 1
                thiscontID += 1

    else:

        if len(not_real_pids) > 0:
            print("Made it to the else in phase test", len(not_real_pids))

        while len(not_real_pids) > 0:

            # Extract halo particle data
            key, val = not_real_pids.popitem()
            this_halo_pids = list(val)
            halo_npart = len(this_halo_pids)
            if halo_npart < 10:
                continue
            this_halo_pos = halo_poss[this_halo_pids, :]
            this_halo_vel = halo_vels[this_halo_pids, :]
            this_sim_halo_pids = sim_halo_pids[this_halo_pids]

            # Compute the centred positions and velocities
            mean_halo_pos = this_halo_pos.mean(axis=0)
            mean_halo_vel = this_halo_vel.mean(axis=0)
            this_halo_pos -= mean_halo_pos
            this_halo_vel -= mean_halo_vel

            # Compute halo's energy
            halo_energy, KE, GE = halo_energy_calc(this_halo_pos,
                                                   this_halo_vel,
                                                   halo_npart,
                                                   pmass, redshift,
                                                   G, h, soft)

            # Get rms radii from the centred position and velocity
            r = hprop.rms_rad(this_halo_pos)
            vr = hprop.rms_rad(this_halo_vel)

            # Compute the velocity dispersion
            veldisp3d, veldisp1d = hprop.vel_disp(this_halo_vel)
            # Define "masses" for property computation
            masses = np.ones(len(this_sim_halo_pids))

            # Compute maximal rotational velocity
            vmax = hprop.vmax(this_halo_pos, masses, G)

            # Calculate half mass radius in position and velocity space
            hmr = hprop.half_mass_rad(this_halo_pos, masses)
            hmvr = hprop.half_mass_rad(this_halo_vel, masses)

            if KE / GE <= 1:

                # Define realness flag
                real = True

            else:

                # Define realness flag
                real = False

            results[thisresultID] = {'pids': this_sim_halo_pids,
                                     'npart': halo_npart, 'real': real,
                                     'mean_halo_pos': mean_halo_pos,
                                     'mean_halo_vel': mean_halo_vel,
                                     'halo_energy': halo_energy,
                                     'KE': KE, 'GE': GE,
                                     "rms_r": r, "rms_vr": vr,
                                     "veldisp3d": veldisp3d,
                                     "veldisp1d": veldisp1d,
                                     "vmax": vmax,
                                     "hmr": hmr,
                                     "hmvr": hmvr}

            thisresultID += 1

    return results


def get_sub_halos(halo_pids, halo_pos, sub_linkl):
    # Do a spatial search for subhalos
    part_subhaloids, assignedsub_parts = find_subhalos(halo_pos, sub_linkl)

    # Get the positions
    subhalo_pids = {}
    for halo in assignedsub_parts:
        subhalo_pids[halo] = halo_pids[list(assignedsub_parts[halo])]

    return subhalo_pids


def hosthalofinder(snapshot, llcoeff, sub_llcoeff, inputpath, savepath,
                   ini_vlcoeff, min_vlcoeff, decrement, verbose, findsubs,
                   ncells, profile, profile_path, cosmo):
    """ Run the halo finder, sort the output results, find subhalos and
        save to a HDF5 file.

    :param snapshot: The snapshot ID.
    :param llcoeff: The host halo linking length coefficient.
    :param sub_llcoeff: The subhalo linking length coefficient.
    :param gadgetpath: The filepath to the gadget simulation data.
    :param batchsize: The number of particle to be queried at one time.
    :param debug_npart: The number of particles to run the program on when
                        debugging.
    :return: None
    """

    # Define MPI message tags
    tags = utilities.enum('READY', 'DONE', 'EXIT', 'START')

    # Ensure the number of cells is <= number of ranks and adjust
    # such that the number of cells is a multiple of the number of ranks
    if ncells < (size - 1):
        ncells = size - 1
    if ncells % size != 0:
        cells_per_rank = int(np.ceil(ncells / size))
        ncells = cells_per_rank * size
    else:
        cells_per_rank = ncells // size

    if verbose and rank == 0:
        print("nCells adjusted to", ncells)

    if profile:
        prof_d = {}
        prof_d["START"] = time.time()
        prof_d["Reading"] = {"Start": [], "End": []}
        prof_d["Domain-Decomp"] = {"Start": [], "End": []}
        prof_d["Communication"] = {"Start": [], "End": []}
        prof_d["Housekeeping"] = {"Start": [], "End": []}
        prof_d["Task-Munging"] = {"Start": [], "End": []}
        prof_d["Host-Spatial"] = {"Start": [], "End": []}
        prof_d["Host-Phase"] = {"Start": [], "End": []}
        prof_d["Sub-Spatial"] = {"Start": [], "End": []}
        prof_d["Sub-Phase"] = {"Start": [], "End": []}
        prof_d["Assigning"] = {"Start": [], "End": []}
        prof_d["Collecting"] = {"Start": [], "End": []}
        prof_d["Writing"] = {"Start": [], "End": []}
    else:
        prof_d = None

    # =============== Domain Decomposition ===============

    read_start = time.time()

    # Open hdf5 file
    hdf = h5py.File(inputpath + "mega_inputs_" + snapshot + ".hdf5", 'r')

    # Get parameters for decomposition
    mean_sep = hdf.attrs['mean_sep']
    boxsize = hdf.attrs['boxsize']
    npart = hdf.attrs['npart']
    redshift = hdf.attrs['redshift']
    pmass = hdf.attrs['pmass']
    h = hdf.attrs['h']

    hdf.close()

    if profile:
        prof_d["Reading"]["Start"].append(read_start)
        prof_d["Reading"]["End"].append(time.time())

    # ============= Compute parameters for candidate halo testing =============

    set_up_start = time.time()

    # Compute the linking length for host halos
    linkl = llcoeff * mean_sep

    # Compute the softening length
    soft = 0.05 * boxsize / npart ** (1. / 3.)

    # Define the gravitational constant
    G = (const.G.to(u.km ** 3 * u.M_sun ** -1 * u.s ** -2)).value

    # Define and convert particle mass to M_sun
    pmass *= 1e10 * 1 / h

    # Compute the linking length for subhalos
    sub_linkl = sub_llcoeff * mean_sep

    # Compute the mean density
    mean_den = npart * pmass * u.M_sun / boxsize ** 3 / u.Mpc ** 3 \
               * (1 + redshift) ** 3
    mean_den = mean_den.to(u.M_sun / u.km ** 3)

    # Define the velocity space linking length
    vlinkl_indp = (np.sqrt(G / 2) * (4 * np.pi * 200 * mean_den / 3) ** (1 / 6)
                   * (1 + redshift) ** 0.5).value

    if profile:
        prof_d["Housekeeping"]["Start"].append(set_up_start)
        prof_d["Housekeeping"]["End"].append(time.time())

    if rank == 0:

        start_dd = time.time()

        # Open hdf5 file
        hdf = h5py.File(inputpath + "mega_inputs_" + snapshot + ".hdf5", 'r')

        # Get positions to perform the decomposition
        pos = hdf['part_pos'][...]

        hdf.close()

        if profile:
            prof_d["Reading"]["Start"].append(read_start)
            prof_d["Reading"]["End"].append(time.time())

        # Build the kd tree with the boxsize argument providing 'wrapping'
        # due to periodic boundaries *** Note: Contrary to cKDTree
        # documentation compact_nodes=False and balanced_tree=False results in
        # faster queries (documentation recommends compact_nodes=True
        # and balanced_tree=True)***
        tree = cKDTree(pos,
                       leafsize=16,
                       compact_nodes=False,
                       balanced_tree=False,
                       boxsize=[boxsize, boxsize, boxsize])

        if verbose:
            print("Domain Decomposition and tree building:",
                  time.time() - start_dd)

            print("Tree memory size", sys.getsizeof(tree), "bytes")

            # print(hp.heap())

    else:

        start_dd = time.time()

        tree = None

    dd_data = utilities.decomp_nodes(npart, size, cells_per_rank, rank)
    thisRank_tasks, thisRank_parts, nnodes, rank_edges = dd_data

    if profile:
        prof_d["Domain-Decomp"]["Start"].append(start_dd)
        prof_d["Domain-Decomp"]["End"].append(time.time())

    comm_start = time.time()

    tree = comm.bcast(tree, root=0)

    if profile:
        prof_d["Communication"]["Start"].append(comm_start)
        prof_d["Communication"]["End"].append(time.time())

    set_up_start = time.time()

    # Get this ranks particles ID "edges"
    low_lim, up_lim = thisRank_parts.min(), thisRank_parts.max() + 1

    # Define this ranks index offset (the minimum particle ID
    # contained in a rank)
    rank_index_offset = thisRank_parts.min()

    if profile:
        prof_d["Housekeeping"]["Start"].append(set_up_start)
        prof_d["Housekeeping"]["End"].append(time.time())

    read_start = time.time()

    # Open hdf5 file
    hdf = h5py.File(inputpath + "mega_inputs_" + snapshot + ".hdf5", 'r')

    # Get the position and velocity of each particle in this rank
    pos = hdf['part_pos'][low_lim: up_lim, :]
    # hdfpos = hdf['part_pos'][...]
    # pos = hdfpos[low_lim: up_lim, :]

    # del hdfpos

    hdf.close()

    if profile:
        prof_d["Reading"]["Start"].append(read_start)
        prof_d["Reading"]["End"].append(time.time())

    # =========================== Find spatial halos ==========================

    start = time.time()

    # Initialise dictionaries for results
    results = {}

    # Initialise task ID counter
    thisTask = 0

    # Loop over this ranks tasks
    while len(thisRank_tasks) > 0:

        # Extract this task particle IDs
        thisTask_parts = thisRank_tasks.pop()

        task_start = time.time()

        # Extract the spatial halos for this tasks particles
        result = spatial_node_task(thisTask,
                                   pos[thisTask_parts - rank_index_offset],
                                   tree, linkl, npart)

        # Store the results in a dictionary for later combination
        results[thisTask] = result

        thisTask += 1

        if profile:
            prof_d["Host-Spatial"]["Start"].append(task_start)
            prof_d["Host-Spatial"]["End"].append(time.time())

    # ================= Combine spatial results across ranks ==================

    combine_start = time.time()

    # Convert to a set for set calculations
    thisRank_parts = set(thisRank_parts)

    comb_data = utilities.combine_tasks_per_thread(results,
                                                   rank,
                                                   thisRank_parts)
    results, halos_in_other_ranks = comb_data

    if profile:
        prof_d["Housekeeping"]["Start"].append(combine_start)
        prof_d["Housekeeping"]["End"].append(time.time())

    if rank == 0:
        print("Spatial search finished", time.time() - start)

    # Collect child process results
    collect_start = time.time()
    collected_results = comm.gather(results, root=0)
    halos_in_other_ranks = comm.gather(halos_in_other_ranks, root=0)

    if profile and rank != 0:
        prof_d["Collecting"]["Start"].append(collect_start)
        prof_d["Collecting"]["End"].append(time.time())

    if rank == 0:

        halos_to_combine = set().union(*halos_in_other_ranks)

        # Combine collected results from children processes into a single dict
        results = {k: v for d in collected_results for k, v in d.items()}

        print(len(results), "spatial halos collected")

        if verbose:
            print("Collecting the results took",
                  time.time() - collect_start, "seconds")

        if profile:
            prof_d["Collecting"]["Start"].append(collect_start)
            prof_d["Collecting"]["End"].append(time.time())

        combine_start = time.time()

        halo_tasks = utilities.combine_tasks_networkx(results,
                                                      size,
                                                      halos_to_combine,
                                                      npart)

        if verbose:
            print("Combining the results took",
                  time.time() - combine_start, "seconds")

        if profile:
            prof_d["Housekeeping"]["Start"].append(combine_start)
            prof_d["Housekeeping"]["End"].append(time.time())

    else:

        halo_tasks = None

    if profile:
        prof_d["Communication"]["Start"].append(comm_start)
        prof_d["Communication"]["End"].append(time.time())

    # ============ Test Halos in Phase Space and find substructure ============

    set_up_start = time.time()

    # Extract this ranks spatial halo dictionaries
    haloID_dict = {}
    subhaloID_dict = {}
    results = {}
    sub_results = {}
    haloID = 0
    subhaloID = 0

    if profile:
        prof_d["Housekeeping"]["Start"].append(set_up_start)
        prof_d["Housekeeping"]["End"].append(time.time())

    if rank == 0:

        count = 0

        # Master process executes code below
        num_workers = size - 1
        closed_workers = 0
        while closed_workers < num_workers:

            # If all other tasks are currently working let the master
            # handle a (fast) low mass halo
            if comm.Iprobe(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG):

                count += 1

                data = comm.recv(source=MPI.ANY_SOURCE,
                                 tag=MPI.ANY_TAG,
                                 status=status)
                source = status.Get_source()
                tag = status.Get_tag()

                if tag == tags.READY:

                    # Worker is ready, so send it a task
                    if len(halo_tasks) != 0:

                        assign_start = time.time()

                        key, thisTask = halo_tasks.popitem()

                        comm.send(thisTask, dest=source, tag=tags.START)

                        if profile:
                            prof_d["Assigning"]["Start"].append(assign_start)
                            prof_d["Assigning"]["End"].append(time.time())

                    else:

                        # There are no tasks left so terminate this process
                        comm.send(None, dest=source, tag=tags.EXIT)

                elif tag == tags.EXIT:

                    closed_workers += 1

            elif len(halo_tasks) > 0 and count > size * 1.5:

                count = 0

                key, thisTask = halo_tasks.popitem()

                if len(thisTask) > 100:

                    halo_tasks[key] = thisTask

                else:

                    read_start = time.time()

                    thisTask.sort()

                    # Open hdf5 file
                    hdf = h5py.File(inputpath +
                                    "mega_inputs_" + snapshot + ".hdf5", 'r')

                    # Get the position and velocity of
                    # each particle in this rank
                    pos = hdf['part_pos'][thisTask, :]
                    vel = hdf['part_vel'][thisTask, :]

                    hdf.close()

                    read_end = time.time()

                    if profile:
                        prof_d["Reading"]["Start"].append(read_start)
                        prof_d["Reading"]["End"].append(read_end)

                    task_start = time.time()

                    # Do the work here
                    result = get_real_host_halos(thisTask, pos, vel, boxsize,
                                                 vlinkl_indp, linkl, pmass,
                                                 ini_vlcoeff, decrement,
                                                 redshift, G, h, soft,
                                                 min_vlcoeff, cosmo)

                    # Save results
                    for res in result:
                        results[(rank, haloID)] = result[res]

                        haloID += 1

                    task_end = time.time()

                    if profile:
                        prof_d["Host-Phase"]["Start"].append(task_start)
                        prof_d["Host-Phase"]["End"].append(task_end)

                    if findsubs:

                        spatial_sub_results = {}

                        # Loop over results getting spatial halos
                        while len(result) > 0:

                            read_start = time.time()

                            key, res = result.popitem()

                            thishalo_pids = np.sort(res["pids"])

                            # Open hdf5 file
                            hdf = h5py.File(inputpath
                                            + "mega_inputs_"
                                            + snapshot + ".hdf5", 'r')

                            # Get the position and velocity of each
                            # particle in this rank
                            subhalo_poss = hdf['part_pos'][thishalo_pids, :]

                            hdf.close()

                            read_end = time.time()

                            if profile:
                                prof_d["Reading"]["Start"].append(read_start)
                                prof_d["Reading"]["End"].append(read_end)

                            task_start = time.time()

                            # Do the work here
                            sub_result = get_sub_halos(thishalo_pids,
                                                       subhalo_poss,
                                                       sub_linkl)

                            while len(sub_result) > 0:
                                key, res = sub_result.popitem()
                                spatial_sub_results[subhaloID] = res

                                subhaloID += 1

                            task_end = time.time()

                            if profile:
                                prof_d["Sub-Spatial"]["Start"].append(
                                    task_start)
                                prof_d["Sub-Spatial"]["End"].append(task_end)

                        # Loop over spatial subhalos
                        while len(spatial_sub_results) > 0:

                            read_start = time.time()

                            key, thisSub = spatial_sub_results.popitem()

                            thisSub.sort()

                            # Open hdf5 file
                            hdf = h5py.File(inputpath
                                            + "mega_inputs_"
                                            + snapshot + ".hdf5", 'r')

                            # Get the position and velocity of each
                            # particle in this rank
                            pos = hdf['part_pos'][thisSub, :]
                            vel = hdf['part_vel'][thisSub, :]

                            hdf.close()

                            read_end = time.time()

                            if profile:
                                prof_d["Reading"]["Start"].append(read_start)
                                prof_d["Reading"]["End"].append(read_end)

                            task_start = time.time()

                            # Do the work here
                            result = get_real_host_halos(thisSub, pos, vel,
                                                         boxsize,
                                                         vlinkl_indp
                                                         * (1600 / 200)
                                                         ** (1 / 6),
                                                         sub_linkl, pmass,
                                                         ini_vlcoeff,
                                                         decrement,
                                                         redshift,
                                                         G, h, soft,
                                                         min_vlcoeff,
                                                         cosmo)

                            # Save results
                            while len(result) > 0:
                                key, res = result.popitem()
                                sub_results[(rank, subhaloID)] = res

                                subhaloID += 1

                            task_end = time.time()

                            if profile:
                                prof_d["Sub-Phase"]["Start"].append(task_start)
                                prof_d["Sub-Phase"]["End"].append(task_end)

            elif len(halo_tasks) == 0:

                data = comm.recv(source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG,
                                 status=status)
                source = status.Get_source()
                tag = status.Get_tag()

                if tag == tags.EXIT:

                    closed_workers += 1

                else:

                    # There are no tasks left so terminate this process
                    comm.send(None, dest=source, tag=tags.EXIT)

    else:

        # ================ Get from master and complete tasks =================

        while True:

            comm.send(None, dest=0, tag=tags.READY)
            thisTask = comm.recv(source=0, tag=MPI.ANY_TAG, status=status)
            tag = status.Get_tag()

            if tag == tags.START:

                read_start = time.time()

                thisTask.sort()

                # Open hdf5 file
                hdf = h5py.File(inputpath
                                + "mega_inputs_"
                                + snapshot + ".hdf5", 'r')

                # Get the position and velocity of each particle in this rank
                pos = hdf['part_pos'][thisTask, :]
                vel = hdf['part_vel'][thisTask, :]

                hdf.close()

                read_end = time.time()

                if profile:
                    prof_d["Reading"]["Start"].append(read_start)
                    prof_d["Reading"]["End"].append(read_end)

                task_start = time.time()

                # Do the work here
                result = get_real_host_halos(thisTask, pos, vel, boxsize,
                                             vlinkl_indp, linkl, pmass,
                                             ini_vlcoeff, decrement, redshift,
                                             G, h, soft, min_vlcoeff, cosmo)

                # Save results
                for res in result:
                    results[(rank, haloID)] = result[res]

                    haloID += 1

                task_end = time.time()

                if profile:
                    prof_d["Host-Phase"]["Start"].append(task_start)
                    prof_d["Host-Phase"]["End"].append(task_end)

                if findsubs:

                    spatial_sub_results = {}

                    # Loop over results getting spatial halos
                    while len(result) > 0:

                        read_start = time.time()

                        key, res = result.popitem()

                        thishalo_pids = np.sort(res["pids"])

                        # Open hdf5 file
                        hdf = h5py.File(inputpath
                                        + "mega_inputs_"
                                        + snapshot + ".hdf5", 'r')

                        # Get the position and velocity of each
                        # particle in this rank
                        subhalo_poss = hdf['part_pos'][thishalo_pids, :]

                        hdf.close()

                        read_end = time.time()

                        if profile:
                            prof_d["Reading"]["Start"].append(read_start)
                            prof_d["Reading"]["End"].append(read_end)

                        task_start = time.time()

                        # Do the work here
                        sub_result = get_sub_halos(thishalo_pids,
                                                   subhalo_poss,
                                                   sub_linkl)

                        while len(sub_result) > 0:
                            key, res = sub_result.popitem()
                            spatial_sub_results[subhaloID] = res

                            subhaloID += 1

                        task_end = time.time()

                        if profile:
                            prof_d["Sub-Spatial"]["Start"].append(task_start)
                            prof_d["Sub-Spatial"]["End"].append(task_end)

                    # Loop over spatial subhalos
                    while len(spatial_sub_results) > 0:

                        read_start = time.time()

                        key, thisSub = spatial_sub_results.popitem()

                        thisSub.sort()

                        # Open hdf5 file
                        hdf = h5py.File(inputpath
                                        + "mega_inputs_"
                                        + snapshot + ".hdf5", 'r')

                        # Get the position and velocity of each
                        # particle in this rank
                        pos = hdf['part_pos'][thisSub, :]
                        vel = hdf['part_vel'][thisSub, :]

                        hdf.close()

                        read_end = time.time()

                        if profile:
                            prof_d["Reading"]["Start"].append(read_start)
                            prof_d["Reading"]["End"].append(read_end)

                        task_start = time.time()

                        # Do the work here
                        result = get_real_host_halos(thisSub, pos, vel,
                                                     boxsize,
                                                     vlinkl_indp
                                                     * (1600 / 200) ** (1 / 6),
                                                     sub_linkl, pmass,
                                                     ini_vlcoeff, decrement,
                                                     redshift, G, h, soft,
                                                     min_vlcoeff, cosmo)

                        # Save results
                        while len(result) > 0:
                            key, res = result.popitem()
                            sub_results[(rank, subhaloID)] = res

                            subhaloID += 1

                        task_end = time.time()

                        if profile:
                            prof_d["Sub-Phase"]["Start"].append(task_start)
                            prof_d["Sub-Phase"]["End"].append(task_end)

            elif tag == tags.EXIT:
                break

        comm.send(None, dest=0, tag=tags.EXIT)

    # Collect child process results
    collect_start = time.time()
    collected_results = comm.gather(results, root=0)
    sub_collected_results = comm.gather(sub_results, root=0)

    if profile and rank != 0:
        prof_d["Collecting"]["Start"].append(collect_start)
        prof_d["Collecting"]["End"].append(time.time())

    if rank == 0:

        print(
            "============================ Halos computed per rank ============================")
        print([len(res) for res in collected_results])
        print(
            "============================ Subhalos computed per rank ============================")
        print([len(res) for res in sub_collected_results])

        newPhaseID = 0
        newPhaseSubID = 0

        phase_part_haloids = np.full((npart, 2), -2, dtype=np.int32)

        # Collect host halo results
        results_dict = {}
        for halo_task in collected_results:
            for halo in halo_task:
                results_dict[(halo, newPhaseID)] = halo_task[halo]
                pids = halo_task[halo]['pids']
                haloID_dict[(halo, newPhaseID)] = newPhaseID
                phase_part_haloids[pids, 0] = newPhaseID
                newPhaseID += 1

        # Collect subhalo results
        sub_results_dict = {}
        for subhalo_task in sub_collected_results:
            for subhalo in subhalo_task:
                sub_results_dict[(subhalo, newPhaseSubID)] = subhalo_task[
                    subhalo]
                pids = subhalo_task[subhalo]['pids']
                subhaloID_dict[(subhalo, newPhaseSubID)] = newPhaseSubID
                phase_part_haloids[pids, 1] = newPhaseSubID
                newPhaseSubID += 1

        if verbose:
            print("Combining the results took", time.time() - collect_start,
                  "seconds")
            print("Results memory size", sys.getsizeof(results_dict), "bytes")
            print("This Rank:", rank)
            # print(hp.heap())

        if profile:
            prof_d["Collecting"]["Start"].append(collect_start)
            prof_d["Collecting"]["End"].append(time.time())

        write_start = time.time()

        # Find the halos with 10 or more particles by finding the unique IDs in the particle
        # halo ids array and finding those IDs that are assigned to 10 or more particles
        unique, counts = np.unique(phase_part_haloids[:, 0],
                                   return_counts=True)
        unique_haloids = unique[np.where(counts >= 10)]

        # Remove the null -2 value for single particle halos
        unique_haloids = unique_haloids[np.where(unique_haloids != -2)]

        # Print the number of halos found by the halo finder in >10, >100, >1000, >10000 criteria
        print(
            "=========================== Phase halos ===========================")
        print(unique_haloids.size, 'halos found with 10 or more particles')
        print(unique[np.where(counts >= 15)].size - 1,
              'halos found with 15 or more particles')
        print(unique[np.where(counts >= 20)].size - 1,
              'halos found with 20 or more particles')
        print(unique[np.where(counts >= 50)].size - 1,
              'halos found with 50 or more particles')
        print(unique[np.where(counts >= 100)].size - 1,
              'halos found with 100 or more particles')
        print(unique[np.where(counts >= 500)].size - 1,
              'halos found with 500 or more particles')
        print(unique[np.where(counts >= 1000)].size - 1,
              'halos found with 1000 or more particles')
        print(unique[np.where(counts >= 10000)].size - 1,
              'halos found with 10000 or more particles')

        # Find the halos with 10 or more particles by finding the unique IDs in the particle
        # halo ids array and finding those IDs that are assigned to 10 or more particles
        unique, counts = np.unique(phase_part_haloids[:, 1],
                                   return_counts=True)
        unique_haloids = unique[np.where(counts >= 10)]

        # Remove the null -2 value for single particle halos
        unique_haloids = unique_haloids[np.where(unique_haloids != -2)]

        # Print the number of halos found by the halo finder in >10, >100, >1000, >10000 criteria
        print(
            "=========================== Phase subhalos ===========================")
        print(unique_haloids.size, 'halos found with 10 or more particles')
        print(unique[np.where(counts >= 15)].size - 1,
              'halos found with 15 or more particles')
        print(unique[np.where(counts >= 20)].size - 1,
              'halos found with 20 or more particles')
        print(unique[np.where(counts >= 50)].size - 1,
              'halos found with 50 or more particles')
        print(unique[np.where(counts >= 100)].size - 1,
              'halos found with 100 or more particles')
        print(unique[np.where(counts >= 500)].size - 1,
              'halos found with 500 or more particles')
        print(unique[np.where(counts >= 1000)].size - 1,
              'halos found with 1000 or more particles')
        print(unique[np.where(counts >= 10000)].size - 1,
              'halos found with 10000 or more particles')

        # ============================= Write out data =============================

        # Set up arrays to store subhalo results
        nhalo = newPhaseID
        halo_nparts = np.full(nhalo, -1, dtype=int)
        mean_poss = np.full((nhalo, 3), -1, dtype=float)
        mean_vels = np.full((nhalo, 3), -1, dtype=float)
        reals = np.full(nhalo, 0, dtype=bool)
        halo_energies = np.full(nhalo, -1, dtype=float)
        KEs = np.full(nhalo, -1, dtype=float)
        GEs = np.full(nhalo, -1, dtype=float)
        nsubhalos = np.zeros(nhalo, dtype=float)
        rms_rs = np.zeros(nhalo, dtype=float)
        rms_vrs = np.zeros(nhalo, dtype=float)
        veldisp1ds = np.zeros((nhalo, 3), dtype=float)
        veldisp3ds = np.zeros(nhalo, dtype=float)
        vmaxs = np.zeros(nhalo, dtype=float)
        hmrs = np.zeros(nhalo, dtype=float)
        hmvrs = np.zeros(nhalo, dtype=float)

        if findsubs:

            # Set up arrays to store host results
            nsubhalo = newPhaseSubID
            subhalo_nparts = np.full(nsubhalo, -1, dtype=int)
            sub_mean_poss = np.full((nsubhalo, 3), -1, dtype=float)
            sub_mean_vels = np.full((nsubhalo, 3), -1, dtype=float)
            sub_reals = np.full(nsubhalo, 0, dtype=bool)
            subhalo_energies = np.full(nsubhalo, -1, dtype=float)
            sub_KEs = np.full(nsubhalo, -1, dtype=float)
            sub_GEs = np.full(nsubhalo, -1, dtype=float)
            host_ids = np.full(nsubhalo, np.nan, dtype=int)
            sub_rms_rs = np.zeros(nsubhalo, dtype=float)
            sub_rms_vrs = np.zeros(nsubhalo, dtype=float)
            sub_veldisp1ds = np.zeros((nsubhalo, 3), dtype=float)
            sub_veldisp3ds = np.zeros(nsubhalo, dtype=float)
            sub_vmaxs = np.zeros(nsubhalo, dtype=float)
            sub_hmrs = np.zeros(nsubhalo, dtype=float)
            sub_hmvrs = np.zeros(nsubhalo, dtype=float)

        else:

            # Set up dummy subhalo results
            subhalo_nparts = None
            sub_mean_poss = None
            sub_mean_vels = None
            sub_reals = None
            subhalo_energies = None
            sub_KEs = None
            sub_GEs = None
            host_ids = None
            sub_rms_rs = None
            sub_rms_vrs = None
            sub_veldisp1ds = None
            sub_veldisp3ds = None
            sub_vmaxs = None
            sub_hmrs = None
            sub_hmvrs = None

        # Create the root group
        snap = h5py.File(savepath + 'halos_' + str(snapshot) + '.hdf5', 'w')

        # Assign simulation attributes to the root of the z=0 snapshot
        snap.attrs[
            'snap_nPart'] = npart  # number of particles in the simulation
        snap.attrs['boxsize'] = boxsize  # box length along each axis
        snap.attrs['part_mass'] = pmass  # particle mass
        snap.attrs['h'] = h  # 'little h' (hubble constant parametrisation)

        # Assign snapshot attributes
        snap.attrs['linking_length'] = linkl  # host halo linking length
        # snap.attrs['rhocrit'] = rhocrit  # critical density parameter
        snap.attrs['redshift'] = redshift
        # snap.attrs['time'] = t

        halo_ids = np.arange(newPhaseID, dtype=int)

        for res in list(results_dict.keys()):
            halo_res = results_dict.pop(res)
            halo_id = haloID_dict[res]
            halo_pids = halo_res['pids']

            mean_poss[halo_id, :] = halo_res['mean_halo_pos']
            mean_vels[halo_id, :] = halo_res['mean_halo_vel']
            halo_nparts[halo_id] = halo_res['npart']
            reals[halo_id] = halo_res['real']
            halo_energies[halo_id] = halo_res['halo_energy']
            KEs[halo_id] = halo_res['KE']
            GEs[halo_id] = halo_res['GE']
            rms_rs[halo_id] = halo_res["rms_r"]
            rms_vrs[halo_id] = halo_res["rms_vr"]
            veldisp1ds[halo_id, :] = halo_res["veldisp1d"]
            veldisp3ds[halo_id] = halo_res["veldisp3d"]
            vmaxs[halo_id] = halo_res["vmax"]
            hmrs[halo_id] = halo_res["hmr"]
            hmvrs[halo_id] = halo_res["hmvr"]

            # Create datasets in the current halo's group in the HDF5 file
            halo = snap.create_group(str(halo_id))  # create halo group
            halo.create_dataset('Halo_Part_IDs', shape=halo_pids.shape,
                                dtype=int,
                                data=halo_pids)  # halo particle ids

        # Save halo property arrays
        snap.create_dataset('halo_IDs',
                            shape=halo_ids.shape,
                            dtype=int,
                            data=halo_ids,
                            compression='gzip')
        snap.create_dataset('mean_positions',
                            shape=mean_poss.shape,
                            dtype=float,
                            data=mean_poss,
                            compression='gzip')
        snap.create_dataset('mean_velocities',
                            shape=mean_vels.shape,
                            dtype=float,
                            data=mean_vels,
                            compression='gzip')
        snap.create_dataset('rms_spatial_radius',
                            shape=rms_rs.shape,
                            dtype=rms_rs.dtype,
                            data=rms_rs,
                            compression='gzip')
        snap.create_dataset('rms_velocity_radius',
                            shape=rms_vrs.shape,
                            dtype=rms_vrs.dtype,
                            data=rms_vrs,
                            compression='gzip')
        snap.create_dataset('1D_velocity_dispersion',
                            shape=veldisp1ds.shape,
                            dtype=veldisp1ds.dtype,
                            data=veldisp1ds,
                            compression='gzip')
        snap.create_dataset('3D_velocity_dispersion',
                            shape=veldisp3ds.shape,
                            dtype=veldisp3ds.dtype,
                            data=veldisp3ds,
                            compression='gzip')
        snap.create_dataset('nparts',
                            shape=halo_nparts.shape,
                            dtype=int,
                            data=halo_nparts,
                            compression='gzip')
        snap.create_dataset('real_flag',
                            shape=reals.shape,
                            dtype=bool,
                            data=reals,
                            compression='gzip')
        snap.create_dataset('halo_total_energies',
                            shape=halo_energies.shape,
                            dtype=float,
                            data=halo_energies,
                            compression='gzip')
        snap.create_dataset('halo_kinetic_energies',
                            shape=KEs.shape,
                            dtype=float,
                            data=KEs,
                            compression='gzip')
        snap.create_dataset('halo_gravitational_energies',
                            shape=GEs.shape,
                            dtype=float,
                            data=GEs,
                            compression='gzip')
        snap.create_dataset('v_max',
                            shape=vmaxs.shape,
                            dtype=vmaxs.dtype,
                            data=vmaxs,
                            compression='gzip')
        snap.create_dataset('half_mass_radius',
                            shape=hmrs.shape,
                            dtype=hmrs.dtype,
                            data=hmrs,
                            compression='gzip')
        snap.create_dataset('half_mass_velocity_radius',
                            shape=hmvrs.shape,
                            dtype=hmvrs.dtype,
                            data=hmvrs,
                            compression='gzip')

        # Assign the full halo IDs array to the snapshot group
        snap.create_dataset('particle_halo_IDs',
                            shape=phase_part_haloids.shape,
                            dtype=int,
                            data=phase_part_haloids,
                            compression='gzip')

        # Get how many halos were found be real
        print("Halos found to initially not be real:",
              halo_ids.size - halo_ids[reals].size, "of", halo_ids.size)

        if findsubs:

            subhalo_ids = np.arange(newPhaseSubID, dtype=int)

            # Create subhalo group
            sub_root = snap.create_group('Subhalos')

            for res in list(sub_results_dict.keys()):
                subhalo_res = sub_results_dict.pop(res)
                subhalo_id = subhaloID_dict[res]
                subhalo_pids = subhalo_res['pids']
                host = np.unique(phase_part_haloids[subhalo_pids, 0])

                assert len(host) == 1, \
                    "subhalo is contained in multiple hosts, " \
                    "this should not be possible"

                sub_mean_poss[subhalo_id, :] = subhalo_res['mean_halo_pos']
                sub_mean_vels[subhalo_id, :] = subhalo_res['mean_halo_vel']
                subhalo_nparts[subhalo_id] = subhalo_res['npart']
                sub_reals[subhalo_id] = subhalo_res['real']
                subhalo_energies[subhalo_id] = subhalo_res['halo_energy']
                sub_KEs[subhalo_id] = subhalo_res['KE']
                sub_GEs[subhalo_id] = subhalo_res['GE']
                host_ids[subhalo_id] = host
                nsubhalos[host] += 1
                sub_rms_rs[subhalo_id] = subhalo_res["rms_r"]
                sub_rms_vrs[subhalo_id] = subhalo_res["rms_vr"]
                sub_veldisp1ds[subhalo_id, :] = subhalo_res["veldisp1d"]
                sub_veldisp3ds[subhalo_id] = subhalo_res["veldisp3d"]
                sub_vmaxs[subhalo_id] = subhalo_res["vmax"]
                sub_hmrs[subhalo_id] = subhalo_res["hmr"]
                sub_hmvrs[subhalo_id] = subhalo_res["hmvr"]

                # Create subhalo group
                subhalo = sub_root.create_group(str(subhalo_id))
                subhalo.create_dataset('Halo_Part_IDs',
                                       shape=subhalo_pids.shape,
                                       dtype=int,
                                       data=subhalo_pids)

            # Save halo property arrays
            sub_root.create_dataset('subhalo_IDs',
                                    shape=subhalo_ids.shape,
                                    dtype=int,
                                    data=subhalo_ids,
                                    compression='gzip')
            sub_root.create_dataset('host_IDs',
                                    shape=host_ids.shape,
                                    dtype=int, data=host_ids,
                                    compression='gzip')
            sub_root.create_dataset('mean_positions',
                                    shape=sub_mean_poss.shape,
                                    dtype=float,
                                    data=sub_mean_poss,
                                    compression='gzip')
            sub_root.create_dataset('mean_velocities',
                                    shape=sub_mean_vels.shape,
                                    dtype=float,
                                    data=sub_mean_vels,
                                    compression='gzip')
            sub_root.create_dataset('rms_spatial_radius',
                                    shape=sub_rms_rs.shape,
                                    dtype=sub_rms_rs.dtype,
                                    data=sub_rms_rs,
                                    compression='gzip')
            sub_root.create_dataset('rms_velocity_radius',
                                    shape=sub_rms_vrs.shape,
                                    dtype=sub_rms_vrs.dtype,
                                    data=sub_rms_vrs,
                                    compression='gzip')
            sub_root.create_dataset('1D_velocity_dispersion',
                                    shape=sub_veldisp1ds.shape,
                                    dtype=sub_veldisp1ds.dtype,
                                    data=sub_veldisp1ds,
                                    compression='gzip')
            sub_root.create_dataset('3D_velocity_dispersion',
                                    shape=sub_veldisp3ds.shape,
                                    dtype=sub_veldisp3ds.dtype,
                                    data=sub_veldisp3ds,
                                    compression='gzip')
            sub_root.create_dataset('nparts',
                                    shape=subhalo_nparts.shape,
                                    dtype=int,
                                    data=subhalo_nparts,
                                    compression='gzip')
            sub_root.create_dataset('real_flag',
                                    shape=sub_reals.shape,
                                    dtype=bool,
                                    data=sub_reals,
                                    compression='gzip')
            sub_root.create_dataset('halo_total_energies',
                                    shape=subhalo_energies.shape,
                                    dtype=float,
                                    data=subhalo_energies,
                                    compression='gzip')
            sub_root.create_dataset('halo_kinetic_energies',
                                    shape=sub_KEs.shape,
                                    dtype=float,
                                    data=sub_KEs,
                                    compression='gzip')
            sub_root.create_dataset('halo_gravitational_energies',
                                    shape=sub_GEs.shape,
                                    dtype=float,
                                    data=sub_GEs,
                                    compression='gzip')
            sub_root.create_dataset('v_max',
                                    shape=sub_vmaxs.shape,
                                    dtype=sub_vmaxs.dtype,
                                    data=sub_vmaxs,
                                    compression='gzip')
            sub_root.create_dataset('half_mass_radius',
                                    shape=sub_hmrs.shape,
                                    dtype=sub_hmrs.dtype,
                                    data=sub_hmrs,
                                    compression='gzip')
            sub_root.create_dataset('half_mass_velocity_radius',
                                    shape=sub_hmvrs.shape,
                                    dtype=sub_hmvrs.dtype,
                                    data=sub_hmvrs,
                                    compression='gzip')

        snap.create_dataset('occupancy',
                            shape=nsubhalos.shape,
                            dtype=nsubhalos.dtype,
                            data=nsubhalos,
                            compression='gzip')

        snap.close()

        if profile:
            prof_d["Writing"]["Start"].append(write_start)
            prof_d["Writing"]["End"].append(time.time())

        # assert -1 not in np.unique(KEs), "halo ids are not sequential!"

    if profile:
        prof_d["END"] = time.time()

        with open(profile_path + "Halo_" + str(rank) + '_'
                  + snapshot + '.pck', 'wb') as pfile:
            pickle.dump(prof_d, pfile)
