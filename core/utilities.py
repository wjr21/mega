import yaml
import readgadgetdata
import h5py
import time
import networkx
from networkx.algorithms.components.connected import connected_components
import numpy as np


def read_param(paramfile):

    # Read in the param file
    with open(paramfile) as yfile:
        parsed_yaml_file = yaml.load(yfile, Loader=yaml.FullLoader)

    # Extract individual dictionaries
    inputs = parsed_yaml_file['inputs']
    flags = parsed_yaml_file['flags']
    params = parsed_yaml_file['parameters']

    return inputs, flags, params


def enum(*sequential, **named):
    """Handy way to fake an enumerated type in Python
    http://stackoverflow.com/questions/36932/how-can-i-represent-an-enum-in-python
    """
    enums = dict(zip(sequential, range(len(sequential))), **named)
    return type('Enum', (), enums)


def to_graph(l):
    """ https://stackoverflow.com/questions/4842613/merge-lists-that-share-common-elements """

    G = networkx.Graph()
    for part in l:
        # each sublist is a bunch of nodes
        G.add_nodes_from(part)
        # it also imlies a number of edges:
        G.add_edges_from(to_edges(part))
    return G

def to_edges(l):
    """ https://stackoverflow.com/questions/4842613/merge-lists-that-share-common-elements
        treat `l` as a Graph and returns it's edges
        to_edges(['a','b','c','d']) -> [(a,b), (b,c),(c,d)]
    """
    it = iter(l)
    last = next(it)

    for current in it:
        yield last, current
        last = current


def binary_to_hdf5(snapshot, PATH, inputpath='input/'):
    """ Reads in gadget-2 simulation data and computes the host halo linking length. (For more information see Docs)

    :param snapshot: The snapshot ID as a string (e.g. '061')
    :param PATH: The filepath to the directory containing the simulation data.
    :param llcoeff: The host halo linking length coefficient.

    :return: pid: An array containing the particle IDs.
             pos: An array of the particle position vectors.
             vel: An array of the particle velocity vectors.
             npart: The number of particles used in the simulation.
             boxsize: The length of the simulation box along a single axis.
             redshift: The redshift of the current snapshot.
             t: The elapsed time of the current snapshot.
             rhocrit: The critical density at the current snapshot.
             pmass: The mass of a dark matter particle.
             h: 'Little h', The hubble parameter parametrisation.
             linkl: The linking length.

    """

    # =============== Load Simulation Data ===============

    # Load snapshot data from gadget-2 file *** Note: will need to be changed for use with other simulations data ***
    snap = readgadgetdata.readsnapshot(snapshot, PATH)
    pid, pos, vel = snap[0:3]  # pid=particle ID, pos=all particle's position, vel=all particle's velocity
    head = snap[3:]  # header values
    npart = head[0]  # number of particles in simulation
    boxsize = head[3]  # simulation box length(/size) along each axis
    redshift = head[1]
    t = head[2]  # elapsed time of the snapshot
    rhocrit = head[4]  # Critical density
    pmass = head[5]  # Particle mass
    h = head[6]  # 'little h' (hubble parameter parametrisation)

    # =============== Sort particles ===============

    # Sort the simulation data arrays by the particle ID
    sinds = pid.argsort()
    pid = pid[sinds]
    pos = pos[sinds, :]
    vel = vel[sinds, :]

    # =============== Compute Linking Length ===============

    # Compute the mean separation
    mean_sep = boxsize / npart**(1./3.)

    # Open hdf5 file
    hdf = h5py.File(inputpath + "mega_inputs_" + snapshot + ".hdf5", 'w')

    # Write out the inputs
    hdf.attrs['mean_sep'] = mean_sep
    hdf.attrs['boxsize'] = boxsize
    hdf.attrs['npart'] = npart
    hdf.attrs['redshift'] = redshift
    hdf.attrs['t'] = t
    hdf.attrs['rhocrit'] = rhocrit
    hdf.attrs['pmass'] = pmass
    hdf.attrs['h'] = h
    hdf.create_dataset('part_pid', shape=pid.shape, dtype=float, data=pid, compression="gzip")
    hdf.create_dataset('sort_inds', shape=sinds.shape, dtype=int, data=sinds, compression="gzip")
    hdf.create_dataset('part_pos', shape=pos.shape, dtype=float, data=pos, compression="gzip")
    hdf.create_dataset('part_vel', shape=vel.shape, dtype=float, data=vel, compression="gzip")

    hdf.close()


def upper_tri_masking(A):
    m = A.shape[0]
    r = np.arange(m)
    mask = r[:, None] <= r
    return A[mask]


def kinetic(halo_vels, halo_npart, redshift, pmass):

    # Compute kinetic energy of the halo
    vel_disp = np.zeros(3, dtype=np.float32)
    for ixyz in [0, 1, 2]:
        vel_disp[ixyz] = np.var(halo_vels[:, ixyz])
    KE = 0.5 * halo_npart * pmass * np.sum(vel_disp) * 1 / (1 + redshift)

    return KE


def grav(rij_2, soft, pmass, redshift, h, G):

    # Compute the sum of the gravitational energy of each particle from
    # GE = G*Sum_i(m_i*Sum_{j<i}(m_j/sqrt(r_{ij}**2+s**2)))
    invsqu_dist = 1 / np.sqrt(rij_2 + soft ** 2)
    GE = G * pmass ** 2 * np.sum(invsqu_dist)

    # Convert GE to be in the same units as KE (M_sun km^2 s^-2)
    GE = GE * h * (1 + redshift) * 1 / 3.086e+19

    return GE


def get_seps_lm(halo_poss, halo_npart):

    # Compute the separations of all halo particles along each dimension
    seps = np.zeros((halo_npart, halo_npart, 3), dtype=np.float32)
    for ixyz in [0, 1, 2]:
        rows, cols = np.atleast_2d(halo_poss[:, ixyz], halo_poss[:, ixyz])
        seps[:, :, ixyz] = rows - cols.T

    # Compute the separation between all particles
    # NOTE: this is a symmetric matrix where we only need the upper right half
    rij2 = np.sum(seps * seps, axis=-1)

    return rij2


def get_grav_hm(halo_poss, halo_npart, soft, pmass, redshift, h, G):

    GE = 0

    for i in range(1, halo_npart):
        sep = (halo_poss[:i, :] - halo_poss[i, :])
        rij2 = np.sum(sep * sep, axis=-1)
        invsqu_dist = np.sum(1 / np.sqrt(rij2 + soft ** 2))

        GE += G * pmass ** 2 * invsqu_dist

    # Convert GE to be in the same units as KE (M_sun km^2 s^-2)
    GE = GE * h * (1 + redshift) * 1 / 3.086e+19

    return GE


def halo_energy_calc_exact(halo_poss, halo_vels, halo_npart, pmass, redshift, G, h, soft):

    # Compute kinetic energy of the halo
    KE = kinetic(halo_vels, halo_npart, redshift, pmass)

    if halo_npart < 10000:

        rij2 = get_seps_lm(halo_poss, halo_npart)

        # Extract only the upper triangle of rij
        rij_2 = upper_tri_masking(rij2)

        # Compute gravitational potential energy
        GE = grav(rij_2, soft, pmass, redshift, h, G)

    else:

        GE = get_grav_hm(halo_poss, halo_npart, soft, pmass, redshift, h, G)

    # Compute halo's energy
    halo_energy = KE - GE

    return halo_energy, KE, GE


def wrap_halo(halo_poss, boxsize, domean=False):

    # Define the comparison particle as the maximum position in the current dimension
    max_part_pos = halo_poss.max(axis=0)

    # Compute all the halo particle separations from the maximum position
    sep = max_part_pos - halo_poss

    # If any separations are greater than 50% the boxsize (i.e. the halo is split over the boundary)
    # bring the particles at the lower boundary together with the particles at the upper boundary
    # (ignores halos where constituent particles aren't separated by at least 50% of the boxsize)
    # *** Note: fails if halo's extent is greater than 50% of the boxsize in any dimension ***
    halo_poss[np.where(sep > 0.5 * boxsize)] += boxsize

    if domean:
        # Compute the shifted mean position in the dimension ixyz
        mean_halo_pos = halo_poss.mean(axis=0)

        # Centre the halos about the mean in the dimension ixyz
        halo_poss -= mean_halo_pos

        return halo_poss, mean_halo_pos

    else:

        return halo_poss


def halo_energy_calc_approx(halo_poss, halo_vels, halo_npart, pmass, redshift, G, h, soft):

    # Compute kinetic energy of the halo
    vel_disp = np.var(halo_vels, axis=0)
    KE = 0.5 * halo_npart * pmass * np.sum(vel_disp) * 1 / (1 + redshift)

    halo_radii = np.sqrt(halo_poss[:, 0]**2 + halo_poss[:, 1]**2 + halo_poss[:, 2]**2)

    srtd_halo_radii = np.sort(halo_radii)

    n_within_radii = np.arange(0, halo_radii.size)
    GE = np.sum(G * pmass**2 * n_within_radii / srtd_halo_radii)

    # Compute halo's energy
    halo_energy = KE - GE * h * (1 + redshift) * 1 / 3.086e+19

    return halo_energy


def bin_nodes(pos, nbins, minmax):

    r0, r1 = minmax

    digitized = (float(nbins) / (r1 - r0) * (pos - r0)).astype(int)
    bin_edges = np.linspace(r0, r1, nbins + 1)

    nodes = {}
    for ind, key in enumerate(digitized):
        nodes.setdefault(tuple(key), set()).update({ind, })

    task_ids = range(len(nodes))

    nodes_labels = dict(zip(task_ids, nodes.keys()))

    return nodes, nodes_labels, bin_edges


def decomp_nodes(npart, nbins):

    # Define bin edges
    bin_edges = np.linspace(0, npart, nbins, dtype=int)

    # Define the nodes
    nodes = {}
    for (ind, low), high in zip(enumerate(bin_edges[:-1]), bin_edges[1:]):
        nodes[ind] = np.arange(low, high, dtype=int)

    return nodes


# def combine_tasks(results, spatial_part_haloids, ini_vlcoeff, nnodes):
#
#     # Initialise halo dictionaries read for the phase space test
#     halo_pids = {}
#     vlcoeffs = {}
#
#     # Store halo ids and halo data for the halos found out in the spatial search
#     newtaskID = nnodes
#     while len(results) > 0:
#
#         parts = results.popitem()[1]
#         uni_part_haloids = np.unique(spatial_part_haloids[parts, 0])
#         uni_part_haloids = uni_part_haloids[uni_part_haloids >= 0]
#
#         if len(uni_part_haloids) == 0:
#
#             # Assign new halo to
#             spatial_part_haloids[parts, 0] = newtaskID
#             halo_pids[(1, newtaskID)] = set(parts)
#
#             newtaskID += 1
#
#         elif len(uni_part_haloids) == 1:
#
#             spatial_part_haloids[parts, 0] = uni_part_haloids[0]
#             halo_pids[(1, uni_part_haloids[np.where(uni_part_haloids != -2)].min())].update(parts)
#
#         else:
#
#             existing_halos = uni_part_haloids
#             final_id = np.min(existing_halos)
#             other_parts = set()
#             for halo in existing_halos:
#                 other_parts.update(halo_pids.pop((1, halo)))
#             other_parts.update(parts)
#             spatial_part_haloids[list(other_parts), 0] = uni_part_haloids[0]
#             halo_pids[(1, final_id)] = other_parts
#
#     # Find the halos with 10 or more particles by finding the unique IDs in the particle
#     # halo ids array and finding those IDs that are assigned to 10 or more particles
#     unique, counts = np.unique(spatial_part_haloids, return_counts=True)
#     unique_haloids = unique[np.where(counts >= 10)]
#
#     # Remove the null -2 value for single particle halos
#     unique_haloids = unique_haloids[np.where(unique_haloids != -2)]
#
#     for ihaloid in unique_haloids:
#
#         # Assign initial vlcoeff
#         vlcoeffs[(1, ihaloid)] = ini_vlcoeff
#
#         pids = list(halo_pids[(1, ihaloid)])
#         halo_pids[(1, ihaloid)] = np.array(pids)
#
#     return halo_pids, vlcoeffs, unique_haloids, spatial_part_haloids, newtaskID


def combine_tasks(results, spatial_part_haloids, ini_vlcoeff, nnodes):

    # Initialise halo dictionaries read for the phase space test
    halo_pids = {}
    vlcoeffs = {}
    start = time.time()
    results = set(results.values())

    G = to_graph(results)
    results = [parts for parts in connected_components(G) if len(parts) >= 10]
    print("Graph", time.time() - start)
    # Store halo ids and halo data for the halos found out in the spatial search
    start = time.time()
    newtaskID = nnodes
    while len(results) > 0:
        parts = np.array(list(results.pop()))

        halo_pids[(1, newtaskID)] = parts

        # Assign initial vlcoeff
        vlcoeffs[(1, newtaskID)] = ini_vlcoeff

        spatial_part_haloids[parts, 0] = newtaskID

        newtaskID += 1

    print("Loop", time.time() - start)

    # Find the halos with 10 or more particles by finding the unique IDs in the particle
    # halo ids array and finding those IDs that are assigned to 10 or more particles
    unique, counts = np.unique(spatial_part_haloids[:, 0], return_counts=True)
    unique_haloids = unique[np.where(counts >= 10)]

    # Remove the null -2 value for single particle halos
    unique_haloids = unique_haloids[np.where(unique_haloids != -2)]

    return halo_pids, vlcoeffs, unique_haloids, spatial_part_haloids, newtaskID


def combine_tasks_per_thread(results, rank, thisRank_parts):
    # Initialise halo dictionaries read for the phase space test
    halo_pids = {}

    results = {parts for d in results.values() for parts in d.values()}

    G = to_graph(results)

    results = list(connected_components(G))

    # Store halo ids and halo data for the halos found out in the spatial search
    newtaskID = 0
    while len(results) > 0:
        parts = results.pop()

        if len(parts) < 10:
            if len(parts - thisRank_parts) == 0:
                continue

        halo_pids[(rank, newtaskID)] = frozenset(parts)
        newtaskID += 1

    return halo_pids


# def combine_tasks_per_thread(results, spatial_part_haloids, rank):
#
#     # Initialise halo dictionaries read for the phase space test
#     halo_pids = {}
#
#     # Store halo ids and halo data for the halos found out in the spatial search
#     newtaskID = 0
#     for task in results:
#         while len(results[task]) > 0:
#
#             parts = results[task].popitem()[1]
#             uni_part_haloids = np.unique(spatial_part_haloids[parts, 0])
#             uni_part_haloids = uni_part_haloids[uni_part_haloids >= 0]
#
#             if len(uni_part_haloids) == 0:
#
#                 # Assign new halo to
#                 spatial_part_haloids[parts, 0] = newtaskID
#                 halo_pids[(rank, newtaskID)] = set(parts)
#
#                 newtaskID += 1
#
#             elif len(uni_part_haloids) == 1:
#
#                 spatial_part_haloids[parts, 0] = uni_part_haloids[0]
#                 halo_pids[(rank, uni_part_haloids[np.where(uni_part_haloids != -2)].min())].update(parts)
#
#             else:
#
#                 existing_halos = uni_part_haloids
#                 final_id = np.min(existing_halos)
#                 other_parts = set()
#                 for halo in existing_halos:
#                     other_parts.update(halo_pids.pop((rank, halo)))
#                 other_parts.update(parts)
#                 spatial_part_haloids[list(other_parts), 0] = uni_part_haloids[0]
#                 halo_pids[(rank, final_id)] = other_parts
#
#     # Find the halos with 10 or more particles by finding the unique IDs in the particle
#     # halo ids array and finding those IDs that are assigned to 10 or more particles
#     unique_haloids, counts = np.unique(spatial_part_haloids, return_counts=True)
#
#     # Remove the null -2 value for single particle halos
#     unique_haloids = unique_haloids[np.where(unique_haloids != -2)]
#
#     for ihaloid in unique_haloids:
#
#         pids = halo_pids.pop((rank, ihaloid))
#         halo_pids[(rank, ihaloid)] = frozenset(pids)
#
#     return halo_pids


def get_linked_halo_data(all_linked_halos, start_ind, nlinked_halos):
    """ A helper function for extracting a halo's linked halos
        (i.e. progenitors and descendants)

    :param all_linked_halos: Array containing all progenitors and descendants.
    :type all_linked_halos: float[N_linked halos]
    :param start_ind: The start index for this halos progenitors or descendents elements in all_linked_halos
    :type start_ind: int
    :param nlinked_halos: The number of progenitors or descendents (linked halos) the halo in question has
    :type nlinked_halos: int
    :return:
    """

    return all_linked_halos[start_ind: start_ind + nlinked_halos]


# binary_to_hdf5(snapshot=snap,
#                PATH='/Users/willroper/Documents/University/Merger_Trees_to_Merger_Graphs/'
#                     'MT2MG_paper_code/snapshotdata/snapdir_')
